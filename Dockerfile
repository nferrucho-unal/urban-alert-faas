# Debian slim provides binary wheels for database and crypto dependencies.
FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias globales del contenedor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente de la función específica
COPY . .

# Variable de entorno para indicarle a Flask qué puerto usar internamente
ENV PORT=5000
ENV PYTHONPATH=/app

# Ejecutar Flask escuchando en todas las interfaces de red (0.0.0.0)
CMD ["sh", "-c", "flask run --host=0.0.0.0 --port=$PORT"]
