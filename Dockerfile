FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# lxml 在 slim 镜像上使用 manylinux wheel，无需编译工具链
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY static /app/static

EXPOSE 8000

# 单进程即可（本地单机工具）；如需多 worker 可自行调整
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
