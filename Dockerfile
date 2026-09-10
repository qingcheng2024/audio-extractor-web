FROM python:3.12-slim

# 安装 ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY web_server.py .
COPY web_index.html .

# Render 会通过环境变量注入 PORT
ENV PORT=10000
EXPOSE ${PORT}

CMD ["python", "web_server.py"]
