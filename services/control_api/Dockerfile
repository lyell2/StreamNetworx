FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx redis
COPY main.py test_control_auth.py test_control_state_sync.py ./
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5060"]
