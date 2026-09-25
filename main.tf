# =========================================================================
# 1. CONFIGURACIÓN DEL PROVEEDOR (AWS)
# =========================================================================
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1" # Región de referencia declarada en el documento
}

# =========================================================================
# 2. PROVEEDOR DE MENSAJERÍA - AMAZON MQ (ADR-01: RabbitMQ gestionado)
# =========================================================================
resource "aws_mq_broker" "urban_alert_mq" {
  broker_name        = "urban-alert-mq-cluster"
  engine_type        = "RabbitMQ"
  engine_version     = "3.13" # Alineado con la versión local probada
  host_instance_type = "mq.m5.large"
  deployment_mode    = "CLUSTER_MULTI_AZ" # Gobernanza: 3 Nodos distribuidos (Pág 27, 31)

  user {
    username = "urban_user"
    password = "urban_secure_password_cloud_2026" # Reemplazar por secretos en prod
  }
}

# =========================================================================
# 3. CACHÉ E IDEMPOTENCIA - AMAZON ELASTICACHE (ADR-04: Redis con Failover)
# =========================================================================
resource "aws_elasticache_replication_group" "urban_alert_cache" {
  replication_group_id       = "urban-alert-redis-group"
  description                = "Almacen de sesion, cache de mapas e idempotencia 24h"
  node_type                  = "cache.t4g.medium"
  num_cache_clusters         = 2 # Failover automático con réplica (Pág 28, 30)
  parameter_group_name       = "default.redis7"
  port                       = 6379
  automatic_failover_enabled = true
}

# =========================================================================
# 4. BASE DE DATOS CORE - AMAZON RDS MULTI-AZ (ADR-05: Redundancia Activa)
# =========================================================================
resource "aws_db_instance" "core_db" {
  identifier             = "urban-alert-core-db"
  allocated_storage      = 20
  max_allocated_storage  = 100 # Gobernanza: Almacenamiento con auto-escalado (Pág 30)
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.m5.large" # Instancia dedicada exigida en el ADR-05
  db_name                = "urban_alert_core"
  username               = "core_admin"
  password               = "core_secure_pass_cloud"
  skip_final_snapshot    = true
  
  # QAS-06: Redundancia Activa mediante réplica en caliente Multi-AZ gestionada
  multi_az               = true 
  storage_encrypted      = true # Cifrado KMS en reposo obligatorio (Pág 28, 29)
}

# =========================================================================
# 5. BASE DE DATOS GEOESPACIAL AISLADA - POSTGRES + POSTGIS (QAS-05: Bulkhead)
# =========================================================================
resource "aws_db_instance" "gis_db" {
  identifier            = "urban-alert-gis-db"
  allocated_storage     = 20
  engine                = "postgres"
  engine_version        = "16"
  instance_class        = "db.t4g.medium" # Instancia e infraestructura aislada del Core
  db_name               = "urban_alert_geo"
  username              = "gis_admin"
  password              = "gis_secure_pass_cloud"
  skip_final_snapshot   = true
  
  # Aislamiento estructural: Las consultas al mapa no tocan la instancia core_db
}

# =========================================================================
# 6. CAPA DE CÓMPUTO FAAS - AWS LAMBDA (Servicio de Notificaciones - QAS-02)
# =========================================================================

# Rol IAM restrictivo para la Lambda (Gobernanza: Solo envíos, sin acceso extra - Pág 29)
resource "aws_iam_role" "lambda_notifications_role" {
  name = "urban-alert-notifications-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "://amazonaws.com" }
    }]
  })
}

# Compresión automática del código fuente local de la función
data "archive_file" "notifications_zip" {
  type        = "zip"
  source_dir  = "${path.module}/fn_notifications"
  output_path = "${path.module}/fn_notifications.zip"
}

resource "aws_lambda_function" "fn_notifications" {
  filename         = data.archive_file.notifications_zip.output_path
  function_name    = "UrbanAlert_FaaS_Notifications"
  role             = aws_iam_role.lambda_notifications_role.arn
  handler          = "app.callback_notificaciones" # Archivo.Función manejadora
  runtime          = "python3.12"
  
  # Gobernanza técnica estricta (Página 27, 29):
  memory_size      = 256  # Rango permitido: 256–512 MB
  timeout          = 10   # Timeout de 10s ante caídas de terceros

  environment {
    variables = {
      BROKER_URL = "amqps://${aws_mq_broker.urban_alert_mq.user[0].username}:${aws_mq_broker.urban_alert_mq.user[0].password}@${aws_mq_broker.urban_alert_mq.instances[0].endpoint}"
      REDIS_URL  = "redis://${aws_elasticache_replication_group.urban_alert_cache.primary_endpoint_address}:6379/0"
    }
  }
}

# =========================================================================
# 7. SERVICIO FAAS DE AUDITORÍA (QAS-04.1 & QAS-04.2)
# =========================================================================

# Rol IAM restrictivo para la Lambda de Auditoría (Gobernanza: Permiso Append-Only)
resource "aws_iam_role" "lambda_audit_role" {
  name = "urban-alert-audit-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "://amazonaws.com" }
    }]
  })
}

# Empaquetado automático del código fuente local de Auditoría
data "archive_file" "audit_zip" {
  type        = "zip"
  source_dir  = "${path.module}/fn_audit"
  output_path = "${path.module}/fn_audit.zip"
}

# Definición de la Función Lambda de Auditoría
resource "aws_lambda_function" "fn_audit" {
  filename         = data.archive_file.audit_zip.output_path
  function_name    = "UrbanAlert_FaaS_Audit"
  role             = aws_iam_role.lambda_audit_role.arn
  handler          = "app.callback_auditoria" # Archivo.Función
  runtime          = "python3.12"
  
  # Parámetros de Gobernanza (Página 29):
  memory_size      = 256  # Configuración rígida de 256 MB
  timeout          = 5    # Timeout ajustado a 5s por evento

  environment {
    variables = {
      BROKER_URL = "amqps://${aws_mq_broker.urban_alert_mq.user.username}:${aws_mq_broker.urban_alert_mq.user.password}@${aws_mq_broker.urban_alert_mq.instances.endpoint}"
      REDIS_URL  = "redis://${aws_elasticache_replication_group.urban_alert_cache.primary_endpoint_address}:6379/0"
    }
  }
}

# =========================================================================
# 8. SERVICIO FAAS MULTIMEDIA (QAS-03 & QAS-08: Procesamiento de Imágenes)
# =========================================================================

# Rol IAM para la Lambda Multimedia
resource "aws_iam_role" "lambda_multimedia_role" {
  name = "urban-alert-multimedia-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "://amazonaws.com" }
    }]
  })
}

# Empaquetado automático del código fuente local Multimedia
data "archive_file" "multimedia_zip" {
  type        = "zip"
  source_dir  = "${path.module}/fn_multimedia"
  output_path = "${path.module}/fn_multimedia.zip"
}

# Definición de la Función Lambda Multimedia
resource "aws_lambda_function" "fn_multimedia" {
  filename         = data.archive_file.multimedia_zip.output_path
  function_name    = "UrbanAlert_FaaS_Multimedia"
  role             = aws_iam_role.lambda_multimedia_role.arn
  handler          = "app.callback_multimedia" # Archivo.Función
  runtime          = "python3.12"
  
  # Parámetros de Gobernanza (Página 30):
  memory_size      = 512  # Requiere más cómputo (512 MB) para procesamiento EXIF y miniaturas
  timeout          = 15   # Timeout de 15s para absorber latencias de manipulación binaria

  environment {
    variables = {
      BROKER_URL = "amqps://${aws_mq_broker.urban_alert_mq.user.username}:${aws_mq_broker.urban_alert_mq.user.password}@${aws_mq_broker.urban_alert_mq.instances.endpoint}"
    }
  }
}
