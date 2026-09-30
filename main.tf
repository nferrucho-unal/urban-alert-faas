terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
data "aws_caller_identity" "current" {}

variable "aws_region" {
  type        = string
  description = "AWS region for Urban Alert."
  default     = "us-east-1"
}

variable "core_backend_image" {
  type        = string
  description = "Public ECR image for the Core API service."
  default     = "public.ecr.aws/urban-alert/core-backend:latest"
}

variable "database_primary_url" {
  type        = string
  sensitive   = true
  description = "Connection URL for the Core PostgreSQL primary. Supply through a secure tfvars/CI secret."
}

variable "redis_host" {
  type        = string
  description = "Primary Redis endpoint used by the Core API."
}

variable "rabbitmq_url" {
  type        = string
  sensitive   = true
  description = "AMQP connection URL used by the Core API to publish domain events."
}

variable "core_vpc_connector_arn" {
  type        = string
  description = "App Runner VPC Connector ARN with egress access to Core PostgreSQL, Redis, and Amazon MQ."
}

variable "waf_rate_limit_per_five_minutes" {
  type        = number
  description = "Maximum requests per client IP in the AWS WAF rolling five-minute window."
  default     = 30000
}

variable "multimedia_cors_origins" {
  type        = list(string)
  description = "Allowed browser origins for direct multipart form uploads to S3."
  default     = ["http://localhost:3000"]
}

variable "gateway_shared_secret" {
  type        = string
  sensitive   = true
  description = "Random secret shared by API Gateway and the Core API; never commit its value."

  validation {
    condition     = length(var.gateway_shared_secret) >= 32
    error_message = "gateway_shared_secret debe tener al menos 32 caracteres aleatorios."
  }
}

resource "aws_s3_bucket" "multimedia" {
  bucket = "urban-alert-evidencias-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

resource "aws_s3_bucket_public_access_block" "multimedia" {
  bucket                  = aws_s3_bucket.multimedia.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "multimedia" {
  bucket = aws_s3_bucket.multimedia.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "multimedia" {
  bucket = aws_s3_bucket.multimedia.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "multimedia" {
  bucket = aws_s3_bucket.multimedia.id

  rule {
    id     = "transition-evidence-after-30-days"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }
    transition {
      days          = 30
      storage_class = "GLACIER"
    }
    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER"
    }
  }

  rule {
    id     = "expire-unconfirmed-uploads"
    status = "Enabled"
    filter {
      prefix = "uploads/pending/"
    }
    expiration {
      days = 1
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "multimedia" {
  bucket = aws_s3_bucket.multimedia.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["POST", "GET", "HEAD"]
    allowed_origins = var.multimedia_cors_origins
    expose_headers  = ["ETag"]
    max_age_seconds = 300
  }
}

resource "aws_iam_role" "core_app_runner_instance" {
  name = "urban-alert-core-app-runner-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "tasks.apprunner.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "core_multimedia_access" {
  name = "urban-alert-multimedia-prefix-access"
  role = aws_iam_role.core_app_runner_instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.multimedia.arn}/reports/*",
          "${aws_s3_bucket.multimedia.arn}/uploads/pending/*",
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.multimedia.arn}/reports/*",
          "${aws_s3_bucket.multimedia.arn}/uploads/pending/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "${aws_s3_bucket.multimedia.arn}/uploads/pending/*"
      },
    ]
  })
}

resource "aws_cognito_user_pool" "urban_alert_identity" {
  name                     = "urban-alert-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }
}

resource "aws_cognito_user_pool_client" "urban_alert_api" {
  name                                          = "urban-alert-api-client"
  user_pool_id                                  = aws_cognito_user_pool.urban_alert_identity.id
  generate_secret                               = false
  explicit_auth_flows                           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors                 = "ENABLED"
  enable_token_revocation                       = true
  enable_propagate_additional_user_context_data = false
}

resource "aws_apprunner_service" "core_app_runner" {
  service_name = "urban-alert-core-service"

  source_configuration {
    image_repository {
      image_identifier      = var.core_backend_image
      image_repository_type = "ECR_PUBLIC"

      image_configuration {
        port = "5000"

        runtime_environment_variables = {
          REDIS_HOST               = var.redis_host
          DATABASE_PRIMARY_URL     = var.database_primary_url
          BROKER_URL               = var.rabbitmq_url
          S3_BUCKET                = aws_s3_bucket.multimedia.bucket
          AWS_REGION               = var.aws_region
          UPLOAD_URL_TTL_SECONDS   = "300"
          DOWNLOAD_URL_TTL_SECONDS = "300"
          COGNITO_ISSUER           = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.urban_alert_identity.id}"
          COGNITO_USER_POOL_ID     = aws_cognito_user_pool.urban_alert_identity.id
          COGNITO_APP_CLIENT_ID    = aws_cognito_user_pool_client.urban_alert_api.id
          GATEWAY_SHARED_SECRET    = var.gateway_shared_secret
          ALLOW_TEST_USER_CONTEXT  = "false"
          REQUEST_TIMEOUT_SECONDS  = "10.0"
        }
      }
    }

    auto_deployments_enabled = true
  }

  instance_configuration {
    cpu               = "1 vCPU"
    memory            = "2 GB"
    instance_role_arn = aws_iam_role.core_app_runner_instance.arn
  }

  network_configuration {
    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = var.core_vpc_connector_arn
    }
  }
}

resource "aws_api_gateway_rest_api" "urban_alert" {
  name        = "urban-alert-public-api"
  description = "Authenticated public ingress for the Urban Alert Core API."

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_authorizer" "cognito_jwt" {
  name            = "urban-alert-cognito-authorizer"
  rest_api_id     = aws_api_gateway_rest_api.urban_alert.id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = [aws_cognito_user_pool.urban_alert_identity.arn]
  identity_source = "method.request.header.Authorization"
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.urban_alert.id
  parent_id   = aws_api_gateway_rest_api.urban_alert.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "root" {
  rest_api_id   = aws_api_gateway_rest_api.urban_alert.id
  resource_id   = aws_api_gateway_rest_api.urban_alert.root_resource_id
  http_method   = "ANY"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito_jwt.id
}

resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.urban_alert.id
  resource_id             = aws_api_gateway_rest_api.urban_alert.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  integration_http_method = "ANY"
  type                    = "HTTP_PROXY"
  uri                     = "https://${aws_apprunner_service.core_app_runner.service_url}/"

  request_parameters = {
    "integration.request.header.X-Urban-Gateway-Key" = "'${var.gateway_shared_secret}'"
  }
}

resource "aws_api_gateway_method" "proxy" {
  rest_api_id   = aws_api_gateway_rest_api.urban_alert.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito_jwt.id

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.urban_alert.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  integration_http_method = "ANY"
  type                    = "HTTP_PROXY"
  uri                     = "https://${aws_apprunner_service.core_app_runner.service_url}/{proxy}"

  request_parameters = {
    "integration.request.path.proxy"                 = "method.request.path.proxy"
    "integration.request.header.X-Urban-Gateway-Key" = "'${var.gateway_shared_secret}'"
  }
}

resource "aws_api_gateway_deployment" "urban_alert" {
  rest_api_id = aws_api_gateway_rest_api.urban_alert.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.root.id,
      aws_api_gateway_integration.root.id,
      aws_api_gateway_method.proxy.id,
      aws_api_gateway_integration.proxy.id,
      aws_api_gateway_integration.root.request_parameters,
      aws_api_gateway_integration.proxy.request_parameters,
      aws_api_gateway_authorizer.cognito_jwt.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "urban_alert" {
  rest_api_id   = aws_api_gateway_rest_api.urban_alert.id
  deployment_id = aws_api_gateway_deployment.urban_alert.id
  stage_name    = "prod"
}

resource "aws_api_gateway_method_settings" "throttling" {
  rest_api_id = aws_api_gateway_rest_api.urban_alert.id
  stage_name  = aws_api_gateway_stage.urban_alert.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = 100
    throttling_burst_limit = 200
    metrics_enabled        = true
  }
}

resource "aws_wafv2_web_acl" "urban_alert" {
  name  = "urban-alert-api-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit-by-client-ip"
    priority = 0

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_five_minutes
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "urban-alert-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-managed-common-rules"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "urban-alert-common-rules"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "urban-alert-api-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "urban_alert_api" {
  resource_arn = aws_api_gateway_stage.urban_alert.arn
  web_acl_arn  = aws_wafv2_web_acl.urban_alert.arn
}

output "urban_alert_api_url" {
  description = "Public API Gateway URL; use this endpoint for client traffic."
  value       = aws_api_gateway_stage.urban_alert.invoke_url
}

output "urban_alert_cognito_user_pool_id" {
  value = aws_cognito_user_pool.urban_alert_identity.id
}

output "urban_alert_cognito_client_id" {
  value = aws_cognito_user_pool_client.urban_alert_api.id
}

output "urban_alert_multimedia_bucket" {
  value = aws_s3_bucket.multimedia.bucket
}