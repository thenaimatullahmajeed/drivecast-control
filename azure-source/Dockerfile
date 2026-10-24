FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 10001 drivecast && useradd --uid 10001 --gid drivecast --create-home drivecast
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY --chown=10001:10001 *.py *.js *.html *.css ./
COPY --chown=10001:10001 *.md ./
COPY --chown=10001:10001 .github ./.github
COPY --chown=10001:10001 azure ./azure
COPY --chown=10001:10001 hosting ./hosting
COPY --chown=10001:10001 tests ./tests
COPY --chown=10001:10001 Dockerfile .dockerignore source-files.json ./
USER 10001:10001
CMD ["python", "app.py", "--accounts", "--port", "8080", "--no-browser"]
