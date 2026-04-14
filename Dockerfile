FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p output temp static/uploads

ENV FONT_BOLD=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
ENV FONT_REGULAR=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

EXPOSE 10000
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "600", "web_app:app"]
