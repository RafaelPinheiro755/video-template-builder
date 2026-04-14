"""
Web interface para o Video Template Builder.
Roda localmente ou em Docker (Render, Railway, etc).
"""

import os
import sys
import re
import time
import uuid
import threading
import subprocess
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB max logo
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "expediente-ai-2026")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

for d in [OUTPUT_DIR, TEMP_DIR, UPLOAD_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Task storage (in-memory, single worker)
tasks = {}
tasks_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=2)

# Mapa de progresso baseado nos prints do script
STAGE_PROGRESS = {
    "[1/5]": (10, "Baixando video..."),
    "[2/5]": (30, "Detectando area util..."),
    "[3/5]": (50, "Cortando video..."),
    "[4/5]": (70, "Montando template..."),
    "[5/5]": (95, "Finalizando..."),
}


def update_task(task_id, **kwargs):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)


def process_video(task_id, params):
    """Roda video_template.py como subprocess e monitora progresso."""
    try:
        update_task(task_id, status="running", progress=5, stage="Iniciando...")

        # Monta comando
        cmd = [sys.executable, str(BASE_DIR / "video_template.py")]

        if params.get("url"):
            cmd += ["--url", params["url"]]
        elif params.get("arquivo"):
            cmd += ["--arquivo", params["arquivo"]]

        cmd += [
            "--perfil", params.get("perfil", ""),
            "--arroba", params.get("arroba", ""),
            "--legenda", params.get("legenda", ""),
            "--titulo", params.get("titulo", ""),
        ]

        # Output com task_id pra nao colidir
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "" for c in params.get("titulo", "video"))
        safe_name = safe_name.strip().replace(" ", "_")[:40]
        output_file = f"{safe_name}_{task_id[:8]}.mp4"
        output_path = OUTPUT_DIR / output_file
        cmd += ["--output", str(output_path)]

        if params.get("logo_path"):
            cmd += ["--foto-perfil", params["logo_path"]]

        # Env com font override pra Docker/Linux
        env = os.environ.copy()

        # Roda subprocess e monitora stdout
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env
        )

        last_lines = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 20:
                last_lines.pop(0)

            # Detecta estagio pelo marcador [X/5]
            for marker, (progress, stage) in STAGE_PROGRESS.items():
                if marker in line:
                    update_task(task_id, progress=progress, stage=stage)
                    break

        proc.wait()

        if proc.returncode != 0:
            error_msg = "\n".join(last_lines[-5:])
            update_task(task_id, status="error", stage="Erro no processamento",
                       error=error_msg)
            return

        if not output_path.exists():
            update_task(task_id, status="error", stage="Arquivo de saida nao encontrado",
                       error="O video nao foi gerado")
            return

        size_mb = output_path.stat().st_size / (1024 * 1024)
        update_task(
            task_id,
            status="done",
            progress=100,
            stage="Concluido!",
            output_file=output_file,
            output_path=str(output_path),
            size_mb=round(size_mb, 1),
        )

    except Exception as e:
        update_task(task_id, status="error", stage="Erro inesperado", error=str(e))

    finally:
        # Limpa temp do task
        for pattern in [f"raw_{task_id}*", f"cropped_{task_id}*"]:
            for f in TEMP_DIR.glob(pattern):
                try:
                    f.unlink()
                except Exception:
                    pass


# --- Cleanup thread ---
def cleanup_old_tasks():
    """Remove tasks e arquivos com mais de 2 horas."""
    while True:
        time.sleep(1800)  # 30 min
        cutoff = time.time() - 7200
        to_delete = []
        with tasks_lock:
            for tid, task in list(tasks.items()):
                if task.get("created_at", 0) < cutoff:
                    to_delete.append(tid)
                    # Remove arquivo de saida
                    out = task.get("output_path")
                    if out:
                        try:
                            Path(out).unlink()
                        except Exception:
                            pass
                    # Remove logo upload
                    logo = task.get("logo_path")
                    if logo:
                        try:
                            Path(logo).unlink()
                        except Exception:
                            pass
            for tid in to_delete:
                del tasks[tid]


cleanup_thread = threading.Thread(target=cleanup_old_tasks, daemon=True)
cleanup_thread.start()


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/process", methods=["POST"])
def api_process():
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL e obrigatoria"}), 400

    perfil = request.form.get("perfil", "").strip()
    arroba = request.form.get("arroba", "").strip()
    legenda = request.form.get("legenda", "").strip()
    titulo = request.form.get("titulo", "").strip()

    if not all([perfil, arroba, legenda, titulo]):
        return jsonify({"error": "Todos os campos sao obrigatorios"}), 400

    task_id = uuid.uuid4().hex[:12]

    # Salva logo se enviada
    logo_path = None
    logo_file = request.files.get("logo")
    if logo_file and logo_file.filename:
        ext = Path(logo_file.filename).suffix or ".png"
        logo_path = str(UPLOAD_DIR / f"logo_{task_id}{ext}")
        logo_file.save(logo_path)

    params = {
        "url": url,
        "perfil": perfil,
        "arroba": arroba,
        "legenda": legenda,
        "titulo": titulo,
        "logo_path": logo_path,
    }

    with tasks_lock:
        tasks[task_id] = {
            "status": "queued",
            "progress": 0,
            "stage": "Na fila...",
            "output_file": None,
            "output_path": None,
            "error": None,
            "size_mb": None,
            "logo_path": logo_path,
            "created_at": time.time(),
        }

    executor.submit(process_video, task_id, params)
    return jsonify({"task_id": task_id})


@app.route("/api/status/<task_id>")
def api_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task nao encontrada"}), 404
    return jsonify({
        "status": task["status"],
        "progress": task["progress"],
        "stage": task["stage"],
        "error": task.get("error"),
        "output_file": task.get("output_file"),
        "size_mb": task.get("size_mb"),
    })


@app.route("/api/download/<task_id>")
def api_download(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task["status"] != "done":
        return jsonify({"error": "Video nao pronto"}), 404
    return send_file(
        task["output_path"],
        as_attachment=True,
        download_name=task["output_file"],
    )


@app.route("/api/preview/<task_id>")
def api_preview(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or task["status"] != "done":
        return jsonify({"error": "Video nao pronto"}), 404
    return send_file(task["output_path"], mimetype="video/mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
