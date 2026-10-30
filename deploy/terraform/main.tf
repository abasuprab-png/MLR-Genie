# ==============================================================================
# MLR Genie — Production Google Cloud Platform (GCP) Terraform Configuration
# Deploys:
# 1. Regional Cloud Run backend with hosted authentication and live Vertex AI
# 2. Cloud SQL PostgreSQL job JSON and private artifact manifests, authenticated through IAM
# 3. Private versioned Google Cloud Storage with soft deletion for artifacts
# 4. Secret Manager containers and scoped runtime access
# 5. External HTTPS load balancer, managed certificate, and Cloud Armor throttling
# ==============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  description = "Target GCP Project ID for MLR Genie"
  type        = string
  default     = "mlr-genie-enterprise-prod"
}

variable "region" {
  description = "Regional deployment and storage location (e.g., us-central1, europe-west1)"
  type        = string
  default     = "us-central1"
}

variable "vertex_ai_location" {
  description = "Vertex inference geography or region; global also requires allow_global_inference=true"
  type        = string
  default     = null
  validation {
    condition = var.vertex_ai_location == null ? true : (
      contains(["us", "eu", "global"], var.vertex_ai_location) ||
      can(regex("^[a-z]+-[a-z]+[0-9]$", var.vertex_ai_location))
    )
    error_message = "vertex_ai_location must be us, eu, global, or a GCP region."
  }
}

variable "allow_global_inference" {
  description = "Explicit approval for global AI processing, which has no US-jurisdiction processing guarantee"
  type        = bool
  default     = false
}

variable "app_image" {
  description = "Immutable container image built from the hosted-authentication fix (repository@sha256:digest)"
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.app_image))
    error_message = "Use the immutable digest of the current application build, not the legacy 3.0.0 image."
  }
}

variable "mount_veeva_credentials" {
  description = "Mount separately provisioned Veeva credentials only when that connector is configured"
  type        = bool
  default     = false
}

variable "runtime_memory" {
  description = "Memory for document processing and request-local artifact staging"
  type        = string
  default     = "2Gi"
}

variable "runtime_request_concurrency" {
  description = "Bound simultaneous PDF jobs per instance to limit memory pressure"
  type        = number
  default     = 4
  validation {
    condition     = var.runtime_request_concurrency >= 1 && var.runtime_request_concurrency <= 1000 && floor(var.runtime_request_concurrency) == var.runtime_request_concurrency
    error_message = "runtime_request_concurrency must be an integer from 1 to 1000."
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Service Account with Least-Privilege IAM
resource "google_service_account" "mlr_genie_runtime_sa" {
  account_id   = "mlr-genie-adk-runtime"
  display_name = "MLR Genie ADK Runtime Service Account"
}

resource "google_project_service" "vertex_ai" {
  project            = var.project_id
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_member" "runtime_vertex_ai" {
  project    = var.project_id
  role       = "roles/aiplatform.user"
  member     = "serviceAccount:${google_service_account.mlr_genie_runtime_sa.email}"
  depends_on = [google_project_service.vertex_ai]
}

# 2. Private Versioned Cloud Storage Bucket for Working Documents & Approved Handoffs
resource "google_storage_bucket" "mlr_artifacts_bucket" {
  name                        = "${var.project_id}-mlr-versioned-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 2592000 # 30 days recovery window
  }
}

# 3. Secret Manager for External Platform Credentials (Outside Prompts & Logs)
resource "google_secret_manager_secret" "veeva_vault_credentials" {
  secret_id = "mlr-genie-veeva-promomats-oauth"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret" "aprimo_credentials" {
  secret_id = "mlr-genie-aprimo-oauth"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

# Secret containers and IAM only. Populate versions out-of-band; never place
# signing/bootstrap values in Terraform variables, resources, plans, or state.
resource "google_secret_manager_secret" "session_signing" {
  secret_id = "mlr-genie-session-signing"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret" "bootstrap" {
  secret_id = "mlr-genie-bootstrap"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "auth_runtime_access" {
  for_each = {
    session   = google_secret_manager_secret.session_signing.secret_id
    bootstrap = google_secret_manager_secret.bootstrap.secret_id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mlr_genie_runtime_sa.email}"
}

# Cloud Run resolves this existing environment secret before starting a revision.
resource "google_secret_manager_secret_iam_member" "veeva_runtime_access" {
  count     = var.mount_veeva_credentials ? 1 : 0
  secret_id = google_secret_manager_secret.veeva_vault_credentials.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mlr_genie_runtime_sa.email}"
}

# 4. Cloud Run Service for MLR Genie ADK Orchestrator & 4-Area Web Application
resource "google_cloud_run_v2_service" "mlr_genie_service" {
  name                 = "mlr-genie-adk-app"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = true
  lifecycle {
    precondition {
      condition     = coalesce(var.vertex_ai_location, var.region) != "global" || var.allow_global_inference
      error_message = "Global AI processing requires explicit allow_global_inference=true approval."
    }
  }
  depends_on = [
    google_secret_manager_secret_iam_member.auth_runtime_access,
    google_secret_manager_secret_iam_member.veeva_runtime_access,
    google_project_service.vertex_ai,
    google_project_iam_member.runtime_vertex_ai,
    google_project_service.cloud_sql_admin,
    google_project_iam_member.runtime_cloud_sql,
    google_sql_database.mlr_genie,
    google_sql_user.runtime_iam,
    google_storage_bucket_iam_member.runtime_artifact_objects,
  ]

  template {
    service_account                  = google_service_account.mlr_genie_runtime_sa.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    max_instance_request_concurrency = var.runtime_request_concurrency

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.mlr_genie.connection_name]
      }
    }

    scaling {
      min_instance_count = 0 # Scales to zero when idle to optimize cost & sustainability
      max_instance_count = 15
    }

    containers {
      image = var.app_image

      resources {
        limits = {
          cpu    = "1"
          memory = var.runtime_memory
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "DATABASE_URL"
        value = local.database_url
      }
      env {
        name  = "MLR_GENIE_DB_IAM_AUTH"
        value = "true"
      }
      env {
        name  = "MLR_GENIE_REQUIRE_DURABLE_STORAGE"
        value = "true"
      }
      env {
        name  = "MLR_GENIE_ARTIFACT_BACKEND"
        value = "gcs"
      }

      env {
        name  = "MLR_GENIE_ENV"
        value = "hosted"
      }
      env {
        name  = "MLR_REQUIRE_SIGNED_BEARER_TOKEN"
        value = "true"
      }
      env {
        name  = "MLR_GENIE_ALLOWED_ORIGINS"
        value = ""
      }
      env {
        name = "MLR_GENIE_SESSION_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_signing.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "MLR_GENIE_BOOTSTRAP_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bootstrap.secret_id
            version = "latest"
          }
        }
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "GOOGLE_GENAI_LIVE_VERTEX"
        value = "true"
      }
      env {
        name  = "MLR_REQUIRE_LIVE_GEMINI"
        value = "true"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = coalesce(var.vertex_ai_location, var.region)
      }
      env {
        name  = "MLR_GCS_BUCKET"
        value = "gs://${google_storage_bucket.mlr_artifacts_bucket.name}"
      }
      dynamic "env" {
        for_each = var.mount_veeva_credentials ? [true] : []
        content {
          name = "VEEVA_OAUTH_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.veeva_vault_credentials.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
}
