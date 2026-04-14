# Video Template Builder

Pipeline automatizado para criar posts de video estilo Instagram/Reels com legenda estilizada.

**Fluxo completo:** URL do video → download → crop automatico → analise com Gemini → legenda com Claude → template 9:16 pronto pra postar.

## Features

- Download de video de qualquer plataforma (Instagram, YouTube, Twitter, LinkedIn, TikTok)
- Deteccao automatica de templates embutidos e barras pretas (analise de movimento temporal)
- Legenda com **negrito** inline renderizada via Pillow (pixel-perfect)
- Tamanho de fonte adaptativo (1 ou 2 linhas, auto-detecta)
- Analise de video com Gemini AI (hash com cenas, objetos, tags)
- Geracao de legenda otimizada com Claude Sonnet (copywriting brasileiro)
- Legenda do post para Instagram gerada automaticamente
- Foto de perfil circular integrada ao template

## Uso rapido

```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar API keys (criar arquivo .env)
echo "GEMINI_API_KEY=sua_key" > .env
echo "ANTHROPIC_API_KEY=sua_key" >> .env

# Pipeline completo (download + analise + legenda AI + template)
python video_template.py \
  --url "https://instagram.com/reel/ABC123" \
  --perfil "MeuPerfil" \
  --arroba "@meuperfil" \
  --analisar \
  --foto-perfil logo.png \
  --abrir

# Com legenda manual
python video_template.py \
  --url "https://youtube.com/watch?v=XXX" \
  --perfil "MeuPerfil" \
  --arroba "@meuperfil" \
  --legenda "A nova **IA** que muda tudo" \
  --foto-perfil logo.png
```

## Requisitos

- Python 3.10+
- ffmpeg (`winget install Gyan.FFmpeg` no Windows, `apt install ffmpeg` no Linux)
- API keys: Gemini (Google AI Studio) + Anthropic (console.anthropic.com)

## Arquivos de saida

```
output/
├── topico_do_video.mp4          # video template pronto
├── topico_do_video_hash.json    # hash completo do Gemini + legendas
└── topico_do_video_post.txt     # legenda do post (pronta pra copiar)
```

## Licenca

MIT
