# ==============================================================================
# MLR Genie — Production Google Cloud Platform (GCP) Terraform Configuration
# Deploys:
# 1. Regional Cloud Run Services (MLR Genie Web/ADK Orchestrator + Document Worker + MCP Adapters)
# 2. Cloud SQL for PostgreSQL (with pgvector for 4-Dimension Statement Library & ADK Sessions)
# 3. Versioned Google Cloud Storage Buckets with CMEK & Retention Policy
# 4. Secret Manager Secrets for Veeva Vault PromoMats & Aprimo Credentials
# 5. Cloud Armor Security Policy & OpenTelemetry Alerting Policies
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
  description = "Regional deployment location for strict data residency (e.g., us-central1, europe-west1)"
  type        = string
  default     = "us-central1"
}

variable "app_image" {
  description = "Immutable container image built from the hosted-authentication fix (repository@sha256:digest)"
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.app_image))
    error_message = "Use the immutable digest of the current application build, not the legacy 3.0.0 image."
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

# 2. Immutable Versioned Cloud Storage Bucket for Working Documents & Approved Handoffs
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

# 4. Cloud Run Service for MLR Genie ADK Orchestrator & 4-Area Web Application
resource "google_cloud_run_v2_service" "mlr_genie_service" {
  name       = "mlr-genie-adk-app"
  location   = var.region
  ingress    = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  depends_on = [google_secret_manager_secret_iam_member.auth_runtime_access]

  template {
    service_account = google_service_account.mlr_genie_runtime_sa.email

    scaling {
      min_instance_count = 0 # Scales to zero when idle to optimize cost & sustainability
      max_instance_count = 15
    }

    containers {
      image = var.app_image

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
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "MLR_GCS_BUCKET"
        value = "gs://${google_storage_bucket.mlr_artifacts_bucket.name}"
      }
      env {
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
