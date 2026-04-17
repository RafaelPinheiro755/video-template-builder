"""
Video Template Builder - downloads video, crops black bars, builds 9:16 template
with profile info + styled caption (supports **bold** markers via Pillow).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import shutil
from pathlib import Path


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Config ---
OUTPUT_DIR = Path(__file__).parent / "output"
TEMP_DIR = Path(__file__).parent / "temp"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CANVAS_W = 1080
CANVAS_H = 1920
VIDEO_W = 1040
VIDEO_Y = 690
CRF = 15

FONTS = {
    "bold": os.environ.get("FONT_BOLD", "C:/Windows/Fonts/segoeuib.ttf"),
    "regular": os.environ.get("FONT_REGULAR", "C:/Windows/Fonts/segoeui.ttf"),
}


def find_ffmpeg():
    """Encontra o ffmpeg no sistema."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"

    search_dirs = [
        Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
        Path.home() / "AppData/Local/CapCut/Apps",
    ]
    for base in search_dirs:
        for p in sorted(base.rglob("ffmpeg.exe"), reverse=True):
            return str(p)

    print("ERRO: ffmpeg nao encontrado. Instale com: winget install Gyan.FFmpeg")
    sys.exit(1)


def find_ytdlp():
    """Encontra o yt-dlp no sistema. Instala se nao tiver."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]

    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass

    print("    yt-dlp nao encontrado, instalando...")
    subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp", "-q"], check=True)
    return [sys.executable, "-m", "yt_dlp"]


def _run_cmd(cmd, error_msg=None, check=False):
    """Run a subprocess and optionally exit on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"ERRO {error_msg}: {result.stderr[-500:]}")
        sys.exit(1)
    return result


def download_video(url, output_path, ffmpeg_path):
    """Baixa video de qualquer URL suportada pelo yt-dlp."""
    print(f"[1/5] Baixando video de {url}...")
    ytdlp = find_ytdlp()
    ffmpeg_dir = str(Path(ffmpeg_path).parent)

    formats = [
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "best",
    ]
    for i, fmt in enumerate(formats):
        cmd = ytdlp + [
            "--ffmpeg-location", ffmpeg_dir,
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            url,
        ]
        if i == 0:
            cmd += ["--no-overwrites", "--retries", "3", "--socket-timeout", "30"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 or output_path.exists():
            break
        if i == 0:
            print("    Tentando fallback com formato simples...")

    # Se o arquivo final nao existe, tenta merge manual dos arquivos parciais
    if not output_path.exists():
        stem = output_path.stem
        parent = output_path.parent
        parts = list(parent.glob(f"{stem}.f*.*"))
        video_part = [p for p in parts if p.suffix == ".mp4"]
        audio_part = [p for p in parts if p.suffix in (".m4a", ".webm")]
        if video_part and audio_part:
            print("    Merge manual video+audio...")
            subprocess.run([
                ffmpeg_path, "-i", str(video_part[0]), "-i", str(audio_part[0]),
                "-c", "copy", "-movflags", "+faststart",
                str(output_path), "-y",
            ], capture_output=True)
            for p in parts:
                try:
                    p.unlink()
                except Exception:
                    pass
        elif video_part:
            video_part[0].rename(output_path)

    if not output_path.exists():
        print(f"ERRO no download: {result.stderr[-500:] if result.stderr else 'arquivo nao gerado'}")
        sys.exit(1)

    print(f"    Salvo em: {output_path}")


def get_video_info(video_path, ffmpeg_path):
    """Pega dimensoes e duracao do video."""
    ffprobe = str(Path(ffmpeg_path).parent / "ffprobe.exe")
    if not Path(ffprobe).exists():
        ffprobe = ffmpeg_path.replace("ffmpeg", "ffprobe")
    result = subprocess.run([
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(video_path),
    ], capture_output=True, text=True)
    info = json.loads(result.stdout)
    for stream in info["streams"]:
        if stream["codec_type"] == "video":
            return {
                "width": int(stream["width"]),
                "height": int(stream["height"]),
                "duration": float(info["format"]["duration"]),
            }
    return None


def detect_content_by_motion(video_path, ffmpeg_path):
    """Detecta a regiao do video real analisando mudanca temporal entre frames.

    Partes estaticas nao mudam entre frames; o conteudo real muda.
    Retorna o crop da area ativa, ou None se nao detectar template embutido.
    """
    info = get_video_info(video_path, ffmpeg_path)
    if not info:
        return None

    w, h = info["width"], info["height"]
    duration = info["duration"]

    if duration < 2:
        return None

    scale_w = 270
    scale_h = int(h * scale_w / w)
    if scale_h < 20:
        return None

    # Extrai 3 frames em momentos diferentes (grayscale)
    frames = []
    for frac in [0.2, 0.5, 0.8]:
        t = max(0.5, duration * frac)
        result = subprocess.run([
            ffmpeg_path, "-ss", str(t), "-i", str(video_path),
            "-vf", f"scale={scale_w}:{scale_h}",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
            "-v", "quiet", "pipe:1"
        ], capture_output=True)
        if len(result.stdout) == scale_w * scale_h:
            frames.append(result.stdout)

    if len(frames) < 2:
        return None

    # Diferenca media por linha entre todos os pares de frames
    row_diff = [0.0] * scale_h
    n_pairs = 0
    for i in range(len(frames)):
        for j in range(i + 1, len(frames)):
            n_pairs += 1
            fi, fj = frames[i], frames[j]
            for y in range(scale_h):
                start = y * scale_w
                row_diff[y] += sum(
                    abs(fj[k] - fi[k]) for k in range(start, start + scale_w)
                ) / scale_w

    row_diff = [d / n_pairs for d in row_diff]

    max_diff = max(row_diff)
    if max_diff < 3:
        return None  # frames quase identicos (slideshow/estatico)

    # Linhas com movimento significativo
    threshold = max(max_diff * 0.15, 2.0)
    active = [y for y in range(scale_h) if row_diff[y] > threshold]

    if not active or len(active) >= scale_h * 0.92:
        return None

    # Mapeia de volta pra coordenadas originais
    scale = h / scale_h
    margin = max(int(h * 0.01), 8)
    y_start = min(int(active[0] * scale) + margin, h - 1)
    y_end = max(int((active[-1] + 1) * scale) - margin, y_start + 1)

    crop_h = (y_end - y_start) & ~1  # divisivel por 2 (requisito codec)

    if crop_h >= h * 0.95:
        return None

    print(f"    Analise de movimento: area ativa de y={y_start} ate y={y_end}")
    print(f"    Crop por movimento: {w}x{crop_h} em x=0, y={y_start}")
    return {"w": w, "h": crop_h, "x": 0, "y": y_start}


def detect_crop(video_path, ffmpeg_path, manual_crop=None):
    """Detecta area util do video (sem barras pretas/templates embutidos)."""
    if manual_crop:
        print(f"[2/5] Usando crop manual: {manual_crop}")
        w, h, x, y = map(int, manual_crop.split(":"))
        return {"w": w, "h": h, "x": x, "y": y}

    print("[2/5] Detectando area util do video...")

    # Fase 1: Analise temporal (detecta templates/overlays embutidos)
    motion_crop = detect_content_by_motion(video_path, ffmpeg_path)
    if motion_crop:
        return motion_crop

    # Fase 2: Cropdetect classico (barras pretas simples)
    info = get_video_info(video_path, ffmpeg_path)
    if not info:
        print("    Nao conseguiu ler info do video")
        return None

    result = subprocess.run([
        ffmpeg_path, "-i", str(video_path),
        "-vf", "cropdetect=24:16:0",
        "-frames:v", "200", "-f", "null", "-"
    ], capture_output=True, text=True)

    crops = {}
    for line in result.stderr.split("\n"):
        if "crop=" in line:
            crop_str = line.split("crop=")[-1].strip()
            crops[crop_str] = crops.get(crop_str, 0) + 1

    if not crops:
        print("    Nenhum crop detectado, usando video inteiro")
        return None

    best_crop = max(crops, key=crops.get)
    w, h, x, y = map(int, best_crop.split(":"))
    print(f"    Cropdetect: {w}x{h} em x={x}, y={y}")
    return {"w": w, "h": h, "x": x, "y": y}


def crop_video(video_path, crop, output_path, ffmpeg_path):
    """Corta o video removendo barras pretas."""
    print("[3/5] Cortando video...")
    _run_cmd([
        ffmpeg_path, "-i", str(video_path),
        "-vf", f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}",
        "-c:v", "libx264", "-crf", str(CRF), "-preset", "fast",
        "-c:a", "copy", "-movflags", "+faststart",
        str(output_path), "-y",
    ], error_msg="no crop", check=True)
    print(f"    Video cortado: {output_path}")


def render_verified_badge(output_path, size=28):
    """Renderiza selo verificado azul do Instagram como PNG transparente."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circulo azul
    draw.ellipse([0, 0, size - 1, size - 1], fill=(0, 149, 246, 255))

    # Checkmark branco
    cx, cy = size / 2, size / 2
    r = size * 0.30
    points = [
        (cx - r * 0.55, cy + r * 0.05),
        (cx - r * 0.10, cy + r * 0.55),
        (cx + r * 0.65, cy - r * 0.45),
    ]
    draw.line(points, fill=(255, 255, 255, 255), width=max(2, size // 10))

    img.save(str(output_path))
    return size


def escape_ffmpeg_text(text):
    """Escapa caracteres especiais pra drawtext do ffmpeg."""
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "'\\\\\\''")
    text = text.replace(":", "\\:")
    text = text.replace("%", "%%")
    return text


def parse_styled_text(text):
    """Parse **bold** markers. Retorna lista de (texto, is_bold)."""
    segments = []
    for part in re.split(r'(\*\*[^*]+\*\*)', text):
        if not part:
            continue
        if part.startswith('**') and part.endswith('**'):
            segments.append((part[2:-2], True))
        else:
            segments.append((part, False))
    return segments or [(text, False)]


def _wrap_words(words, font, font_big, space_w, max_width):
    """Quebra palavras em linhas usando medicao real do Pillow."""
    lines = []
    current_line = []
    current_w = 0.0
    for word, is_bold in words:
        f = font_big if is_bold else font
        word_w = f.getlength(word)
        needed = word_w + (space_w if current_line else 0)
        if current_w + needed > max_width and current_line:
            lines.append(current_line)
            current_line = [(word, is_bold)]
            current_w = word_w
        else:
            current_line.append((word, is_bold))
            current_w += needed
    if current_line:
        lines.append(current_line)
    return lines or [[(words[0][0] if words else "", False)]]


# Modelos de legenda: 1 linha = grande, 2 = medio, 3 = menor
LEGENDA_MODELS = {
    1: {"fontsize": 55, "bold_ratio": 1.18, "line_h_ratio": 1.40},
    2: {"fontsize": 44, "bold_ratio": 1.18, "line_h_ratio": 1.35},
}


def render_legenda_png(legenda, fontsize_hint, max_width, font_bold_path, output_path):
    """Renderiza legenda com **negrito** como PNG transparente via Pillow.

    Detecta automaticamente quantas linhas o texto precisa e aplica
    o modelo de tamanho ideal (1 linha=grande, 2=medio, 3=menor).
    Maximo 3 linhas — se nao couber, reduz ate caber.
    """
    from PIL import Image, ImageDraw, ImageFont

    segments = parse_styled_text(legenda)
    words = [(w, b) for text, b in segments for w in text.split()]
    has_bold = any(b for _, b in words)
    color_normal = (187, 187, 187, 255) if has_bold else (221, 221, 221, 255)
    color_bold = (255, 255, 255, 255)

    # Testa do maior pro menor ate caber em no maximo 2 linhas
    chosen = None
    for n_lines in (1, 2):
        m = LEGENDA_MODELS[n_lines]
        fs = m["fontsize"]
        bold_size = int(fs * m["bold_ratio"])
        font = ImageFont.truetype(font_bold_path, fs)
        font_big = ImageFont.truetype(font_bold_path, bold_size)
        space_w = font.getlength(" ")
        lines = _wrap_words(words, font, font_big, space_w, max_width)
        if len(lines) <= n_lines:
            line_h = int(bold_size * m["line_h_ratio"])
            chosen = (fs, bold_size, font, font_big, space_w, line_h, lines)
            break

    # Fallback: se 2 linhas nao couber, usa modelo 2 e corta
    if not chosen:
        m = LEGENDA_MODELS[2]
        fs = m["fontsize"]
        bold_size = int(fs * m["bold_ratio"])
        font = ImageFont.truetype(font_bold_path, fs)
        font_big = ImageFont.truetype(font_bold_path, bold_size)
        space_w = font.getlength(" ")
        lines = _wrap_words(words, font, font_big, space_w, max_width)
        lines = lines[:2]
        line_h = int(bold_size * m["line_h_ratio"])
        chosen = (fs, bold_size, font, font_big, space_w, line_h, lines)

    fontsize, bold_size, font, font_big, space_w, line_h, lines = chosen
    n = len(lines)
    print(f"    Legenda: modelo {n} linha{'s' if n > 1 else ''} (font={fontsize}, bold={bold_size})")

    # Renderiza
    img_h = max(n * line_h, line_h)
    img = Image.new("RGBA", (int(max_width), img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Calcula baseline real de cada fonte pra alinhar pela parte de baixo
    bbox_big = font_big.getbbox("Ag")
    bbox_normal = font.getbbox("Ag")
    baseline_big = bbox_big[3]     # bottom da fonte grande
    baseline_normal = bbox_normal[3]  # bottom da fonte normal

    for li, line_words in enumerate(lines):
        y_line = li * line_h
        x = 0.0
        for word, is_bold in line_words:
            f = font_big if is_bold else font
            color = color_bold if is_bold else color_normal
            # Alinha pela parte de baixo: desloca normal pra baixo
            y_offset = (baseline_big - baseline_normal) if not is_bold else 2
            draw.text((x, y_line + y_offset), word, font=f, fill=color)
            x += f.getlength(word) + space_w

    img.save(str(output_path))
    return n, line_h


def analyze_video(video_path, output_json=None):
    """Analisa video com Gemini e retorna descricao completa + legenda sugerida."""
    import time
    import google.generativeai as genai

    print("[AI] Analisando video com Gemini...")
    genai.configure(api_key=GEMINI_KEY)

    # Upload do video pro Gemini
    video_file = genai.upload_file(str(video_path))
    while video_file.state.name == "PROCESSING":
        time.sleep(2)
        video_file = genai.get_file(video_file.name)

    if video_file.state.name == "FAILED":
        print("ERRO: Gemini falhou ao processar o video")
        return None

    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content([
        video_file,
        """Analise este video em detalhes e retorne APENAS um JSON valido (sem markdown):
{
  "descricao_geral": "resumo de 2-3 frases do que acontece no video",
  "gancho": "o elemento MAIS surpreendente, contraintuitivo ou impressionante do video em 1 frase curta. O que faria alguem parar de scrollar.",
  "cenas": [
    {"timestamp": "0:00-0:05", "descricao": "o que acontece nesse trecho", "objetos": ["lista", "de", "objetos"]}
  ],
  "tags": ["palavras", "chave", "do", "conteudo"],
  "tom": "informativo/humor/inspirador/chocante/etc"
}"""
    ])

    try:
        # Limpa resposta (remove markdown code blocks se tiver)
        text = response.text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        analysis = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        print(f"    Resposta bruta: {response.text[:500]}")
        return None

    # Salva JSON
    if output_json:
        Path(output_json).write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    Analise salva em: {output_json}")

    def _safe(s):
        return s.encode("ascii", errors="replace").decode()

    print(f"    Descricao: {_safe(analysis.get('descricao_geral', '')[:100])}...")
    print(f"    Tags: {_safe(', '.join(analysis.get('tags', [])))}")

    # Cleanup
    try:
        genai.delete_file(video_file.name)
    except Exception:
        pass

    return analysis


def generate_legenda(analysis, instrucoes=None):
    """Gera legenda do template + legenda do post + topico com Claude Sonnet."""
    import anthropic

    print("[AI] Gerando legendas com Claude Sonnet...")
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)

    ficha = f"""GANCHO: {analysis.get('gancho', 'nao identificado')}
DESCRICAO: {analysis.get('descricao_geral', '')}
TOM: {analysis.get('tom', 'informativo')}
TAGS: {', '.join(analysis.get('tags', [])[:6])}"""

    system_prompt = """Voce e o copywriter do @expediente.ai, perfil brasileiro de tecnologia e IA no Instagram. Publico: brasileiros curiosos por inovacao, tech e futuro. Tom: conversa inteligente entre amigos que entendem de tech, nunca panfleto.

Estilo Light Copy: premissas logicas verdadeiras + ironia sutil + detalhes sensoriais. Parece conversa, nunca marketing.

Retorne APENAS JSON valido, sem markdown:

{
  "topico": "2-4 palavras do assunto (ex: mao robotica chinesa). Sem acentos, minusculo.",
  "legenda_template": "frase curta pro template do video. Max 60 chars sem **. Use **negrito** em 1-2 palavras. Prefira 1 linha.",
  "legenda_post": "legenda completa do post no Instagram. Siga TODAS as regras abaixo."
}

REGRAS DA LEGENDA_TEMPLATE:
- Menos e mais. Menor quantidade de palavras possivel.
- Prefira 1 linha. So use 2 se o impacto exigir.
- Max 60 caracteres (sem **). Conte antes de responder.
- Sem emoji, hashtag, CTA, ponto final.
- Seja INFORMATIVA: diga O QUE acontece no video com especificidade.
- NUNCA clickbait vazio ou frases de efeito sem conteudo.
- O publico e GERAL, nao segue a pagina. Use linguagem acessivel.
- Prefira termos universais ("mao robotica chinesa") a nomes tecnicos ("Revo 3 da BrainCo"). O leitor nao conhece a marca.
- A legenda deve ser auto-explicativa pra quem nunca viu o perfil.
- NUNCA use travessao (—) na legenda do template. Use virgula ou ponto se precisar.
- Se o video mostra um lancamento/novidade, use "novo", "acabou de sair", "lancou".
- Bom: "A **mao robotica** chinesa que sente texturas sem cameras"
- Ruim: "A **Revo 3** chinesa resolve cubo magico" (ninguem sabe o que e Revo 3)
- Ruim: "IA que vende **qualquer coisa** — ate isso" (travessao proibido)

REGRAS DA LEGENDA_POST:

ESTRUTURA (Hook > Corpo > CTA > Hashtags):

1. HOOK (primeira linha, max 80 chars):
   Essa linha aparece ANTES do "ver mais". Unica chance de prender.
   Formatos que funcionam:
   - Numero chocante: "Essa IA custou US$100 milhoes. O resultado? Assustador."
   - Confessional: "Testei 47 ferramentas de IA. 44 eram inuteis."
   - Provocacao: "Voce esta usando IA errado. E nem sabe."
   - Segredo: "Ninguem te conta isso sobre o ChatGPT..."
   - Transformacao: "Ha 6 meses eu levava 3h. Agora levo 30min com IA."
   NUNCA comece com "Descubra", "Confira", "Neste video".

2. CORPO (3-4 paragrafos curtos):
   - Micro-storytelling ou dados concretos.
   - Pelo menos 1 numero/nome/metrica especifica.
   - Linguagem em 1a pessoa quando possivel.
   - Cada paragrafo = 1-2 frases. Nunca blocos longos.
   - Tom: como se estivesse contando pro amigo no bar.
   - Publico GERAL que nao segue a pagina. Explique termos tecnicos. Use nomes de marcas/produtos so no corpo, nunca no hook. No hook use descricao acessivel ("mao robotica chinesa" em vez de "Revo 3 da BrainCo").

3. CTA (1 linha antes das hashtags):
   - Incentive SAVE ou SHARE (sinais mais fortes pro algoritmo).
   - Especifico: "Salva pra testar depois" / "Manda pra quem acha que IA e modinha"
   - NUNCA: "Curta e compartilhe" / "Ative o sininho"

4. HASHTAGS (3-5, no final):
   - Nicho-especificas: #inteligenciaartificial #robotica
   - NUNCA genericas: #viral #trending #tech

FORMATACAO CRITICA:
- Separe paragrafos com LINHA EM BRANCO real (nao use \\n, use espaco real entre blocos)
- Paragrafos de 1-2 frases max
- Zero emoji (marca do @expediente.ai)
- NUNCA use travessao (—). Use virgula ou ponto.
- Sem ponto final na ultima frase antes das hashtags

EXEMPLO BOM DE LEGENDA_POST:

A China criou uma mao robotica que sente a diferenca entre uma uva e uma bola de gude.

12 sensores por dedo. Tempo de resposta de 5 milissegundos. Pressao ajustavel de 0.1 a 50 newtons.

O detalhe mais insano: ela aprende texturas novas sozinha. Toca um objeto uma vez e calibra a pressao ideal pra sempre.

Daqui a 5 anos, cirurgioes podem operar de outro continente com maos assim

Manda pra alguem que ainda acha que robo so serve pra montar carro

#robotica #inteligenciaartificial #china #tecnologia #inovacao"""

    user_msg = f"Ficha do video:\n{ficha}"
    if instrucoes:
        user_msg += f"\n\nInstrucoes adicionais:\n{instrucoes}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )

    try:
        text = response.content[0].text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        result = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        # Fallback: trata como legenda simples
        legenda = response.content[0].text.strip().strip('"').strip("'")
        return {"topico": "video", "legenda_template": legenda, "legenda_post": ""}

    # Valida tamanho da legenda_template
    template = result.get("legenda_template", "")
    clean_len = len(re.sub(r'\*\*', '', template))
    if clean_len > 60:
        print(f"    Legenda longa ({clean_len} chars), pedindo mais curta...")
        retry = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="Reescreva esta legenda de template em no maximo 60 caracteres (sem contar **). Menos e mais. Retorne APENAS a frase.",
            messages=[{"role": "user", "content": template}],
        )
        result["legenda_template"] = retry.content[0].text.strip().strip('"').strip("'")

    print(f"    Topico: {result.get('topico', '')}")
    print(f"    Template: {result.get('legenda_template', '')}")
    print(f"    Post: {result.get('legenda_post', '')[:80]}...")
    return result


def build_template(video_path, output_path, ffmpeg_path, perfil, arroba, legenda, foto_perfil=None):
    """Monta o template 9:16 com fundo preto + textos + video.

    A legenda suporta **negrito** inline pra dar enfase em palavras.
    """
    print("[4/5] Montando template...")

    perfil_esc = escape_ffmpeg_text(perfil)
    arroba_esc = escape_ffmpeg_text(arroba)

    font_bold = FONTS["bold"].replace(":", "\\:")
    font_regular = FONTS["regular"].replace(":", "\\:")

    PFP_SIZE = 96
    PFP_X = 90
    PFP_Y = 365
    TEXT_X = PFP_X + PFP_SIZE + 18
    TEXT_Y_NOME = 378
    TEXT_Y_ARROBA = 420
    TEXT_Y_LEGENDA = PFP_Y + PFP_SIZE + 28

    max_text_w = CANVAS_W - PFP_X * 2

    # Renderiza legenda como PNG (auto-seleciona modelo 1/2/3 linhas)
    legenda_png = TEMP_DIR / "legenda_text.png"
    render_legenda_png(legenda, None, max_text_w, FONTS["bold"], legenda_png)

    # Build inputs and filter_complex - shared between both branches
    inputs = [
        "-f", "lavfi", "-i", f"color=c=black:s={CANVAS_W}x{CANVAS_H}",
        "-i", str(video_path),
    ]

    if foto_perfil:
        inputs += ["-i", str(foto_perfil)]  # [2]
        inputs += ["-i", str(legenda_png)]  # [3]
        leg_idx = 3
        r = PFP_SIZE // 2
        circle_mask = f"if(lte(pow(X-{r}\\,2)+pow(Y-{r}\\,2)\\,pow({r}\\,2))\\,255\\,0)"
        pfp_filter = (
            f"[2:v]scale='if(gte(iw/ih\\,1)\\,{PFP_SIZE}\\,{PFP_SIZE}*iw/ih)':'if(gte(iw/ih\\,1)\\,{PFP_SIZE}*ih/iw\\,{PFP_SIZE})',"
            f"pad={PFP_SIZE}:{PFP_SIZE}:(ow-iw)/2:(oh-ih)/2:black,format=yuva444p,"
            f"geq=lum='p(X\\,Y)':cb='p(X\\,Y)':cr='p(X\\,Y)':a='{circle_mask}'[pfp];"
        )
        bg_init = f"[0:v][vid]overlay=20:{VIDEO_Y}:shortest=1[bg];"
        pfp_overlay = f"[bg][pfp]overlay={PFP_X}:{PFP_Y}[bg2];"
    else:
        inputs += ["-i", str(legenda_png)]  # [2]
        leg_idx = 2
        pfp_filter = ""
        bg_init = (
            f"[0:v]drawbox=x={PFP_X}:y={PFP_Y}:w={PFP_SIZE}:h={PFP_SIZE}:color=0x444444:t=fill[bg];"
            f"[bg][vid]overlay=20:{VIDEO_Y}:shortest=1[bg2];"
        )
        pfp_overlay = ""

    NOME_FONTSIZE = 36

    # Renderiza selo verificado alinhado ao centro das maiusculas
    from PIL import ImageFont
    badge_png = TEMP_DIR / "verified_badge.png"
    badge_size = render_verified_badge(badge_png, size=28)
    nome_font = ImageFont.truetype(FONTS["bold"], NOME_FONTSIZE)
    nome_width = int(nome_font.getlength(perfil))
    ascent, _ = nome_font.getmetrics()
    cap_center = TEXT_Y_NOME + ascent * 0.38
    BADGE_X = TEXT_X + nome_width + 8
    BADGE_Y = int(cap_center - badge_size / 2)

    # Adiciona badge como input (indice = numero de inputs existentes)
    badge_idx = sum(1 for v in inputs if v == "-i")
    inputs += ["-i", str(badge_png)]

    drawtext = (
        f"drawtext=fontfile='{font_bold}':text='{perfil_esc}':fontsize={NOME_FONTSIZE}:fontcolor=white:x={TEXT_X}:y={TEXT_Y_NOME},"
        f"drawtext=fontfile='{font_regular}':text='{arroba_esc}':fontsize=30:fontcolor=0x888888:x={TEXT_X}:y={TEXT_Y_ARROBA}"
    )

    filter_complex = (
        f"{pfp_filter}"
        f"[1:v]scale={VIDEO_W}:-2[vid];"
        f"{bg_init}"
        f"{pfp_overlay}"
        f"[bg2][{leg_idx}:v]overlay={PFP_X}:{TEXT_Y_LEGENDA}[base];"
        f"[base]{drawtext}[named];"
        f"[named][{badge_idx}:v]overlay={BADGE_X}:{BADGE_Y}"
    )

    _run_cmd([ffmpeg_path] + inputs + [
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-crf", str(CRF), "-preset", "fast",
        "-c:a", "copy", "-map", "1:a?",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path), "-y",
    ], error_msg="na montagem", check=True)
    print(f"    Template montado: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Video Template Builder - cria posts estilo Instagram")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="URL do video (Instagram, Twitter, YouTube, etc)")
    source.add_argument("--arquivo", help="Caminho do video local")

    parser.add_argument("--perfil", required=True, help="Nome do perfil (ex: 'Evolving AI')")
    parser.add_argument("--arroba", required=True, help="@ do perfil (ex: '@evolving.ai')")
    parser.add_argument("--legenda", default=None, help="Legenda (suporta **negrito**). Se omitido com --analisar, usa a sugerida pela IA")
    parser.add_argument("--output", help="Caminho do arquivo de saida", default=None)
    parser.add_argument("--foto-perfil", help="Caminho da foto de perfil/logo (ex: logo.png)")
    parser.add_argument("--crop-manual", help="Crop manual no formato W:H:X:Y (ex: 720:390:0:460)")
    parser.add_argument("--no-crop", action="store_true", help="Pula o crop automatico")
    parser.add_argument("--analisar", action="store_true", help="Analisa o video com Gemini AI e gera hash/descricao")
    parser.add_argument("--instrucoes", default=None, help="Instrucoes extras pro Claude gerar a legenda (ex: 'foque no humor')")
    parser.add_argument("--abrir", action="store_true", help="Abre o video ao finalizar")

    args = parser.parse_args()

    # Setup
    ffmpeg_path = find_ffmpeg()
    TEMP_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Passo 1: Obter video
    if args.url:
        raw_video = TEMP_DIR / "raw_download.mp4"
        download_video(args.url, raw_video, ffmpeg_path)
    else:
        raw_video = Path(args.arquivo)
        if not raw_video.exists():
            print(f"ERRO: arquivo nao encontrado: {raw_video}")
            sys.exit(1)
        print(f"[1/5] Usando arquivo local: {raw_video}")

    # Passo 1.5: Analise com Gemini (se pedido)
    analysis = None
    if args.analisar:
        analysis_json = OUTPUT_DIR / f"analise_{raw_video.stem}.json"
        analysis = analyze_video(raw_video, output_json=str(analysis_json))

    # Legenda: usa a fornecida ou gera com Claude a partir do hash
    legenda = args.legenda
    topico = None
    legenda_post = None
    if not legenda and analysis:
        result = generate_legenda(analysis, instrucoes=args.instrucoes)
        legenda = result.get("legenda_template", "")
        topico = result.get("topico", "video")
        legenda_post = result.get("legenda_post", "")
    if not legenda:
        print("ERRO: --legenda e obrigatorio (ou use --analisar pra gerar automaticamente)")
        sys.exit(1)

    # Nome base dos arquivos (topico ou legenda)
    if topico:
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "" for c in topico)
    else:
        safe_name = re.sub(r'\*\*([^*]+)\*\*', r'\1', legenda)
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "" for c in safe_name)
    safe_name = safe_name.strip().replace(" ", "_")[:50] or "output"

    # Salva arquivos com nome do topico
    if analysis and topico:
        old_json = OUTPUT_DIR / f"analise_{raw_video.stem}.json"
        new_json = OUTPUT_DIR / f"{safe_name}_hash.json"
        if old_json.exists():
            if legenda_post:
                analysis["legenda_post"] = legenda_post
                analysis["legenda_template"] = legenda
                old_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            old_json.replace(new_json)
            print(f"    Hash salvo: {new_json.name}")

        # Salva legenda do post como .txt com quebras reais (pronto pra copiar)
        if legenda_post:
            post_txt = OUTPUT_DIR / f"{safe_name}_post.txt"
            post_txt.write_text(legenda_post, encoding="utf-8")
            print(f"    Legenda post: {post_txt.name}")

    # Passo 2-3: Crop
    if args.no_crop:
        cropped_video = raw_video
        print("[2/5] Crop desativado")
        print("[3/5] Pulando...")
    else:
        crop = detect_crop(raw_video, ffmpeg_path, manual_crop=args.crop_manual)
        if crop:
            cropped_video = TEMP_DIR / "cropped.mp4"
            crop_video(raw_video, crop, cropped_video, ffmpeg_path)
        else:
            cropped_video = raw_video

    # Passo 4: Template
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = OUTPUT_DIR / f"{safe_name}.mp4"

    build_template(cropped_video, output_path, ffmpeg_path, args.perfil, args.arroba, legenda, foto_perfil=args.foto_perfil)

    # Passo 5: Resultado
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n[5/5] Concluido!")
    print(f"    Arquivo: {output_path}")
    print(f"    Tamanho: {size_mb:.1f} MB")
    print(f"    Resolucao: {CANVAS_W}x{CANVAS_H}")

    # Limpar temp
    for f in TEMP_DIR.glob("*"):
        try:
            f.unlink()
        except Exception:
            pass

    # Abrir
    if args.abrir:
        os.startfile(str(output_path))


if __name__ == "__main__":
    main()
