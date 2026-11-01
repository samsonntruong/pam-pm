FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir . \
  && useradd -m -u 10001 pam \
  && mkdir -p /data && chown pam:pam /data
ENV PAM_HOME=/data
USER pam
VOLUME /data
EXPOSE 8765
# Setup token and setup URL are printed in the logs at start.
ENTRYPOINT ["pam", "start", "--no-browser", "--host", "0.0.0.0"]
