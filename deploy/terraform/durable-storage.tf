variable "database_tier" {
  description = "Cloud SQL machine tier; the default reserves two vCPUs and 7.5 GiB RAM for production"
  type        = string
  default     = "db-custom-2-7680"
}

variable "database_availability_type" {
  description = "REGIONAL uses a standby zone; ZONAL is available for disposable development environments"
  type        = string
  default     = "REGIONAL"
  validation {
    condition     = contains(["REGIONAL", "ZONAL"], var.database_availability_type)
    error_message = "database_availability_type must be REGIONAL or ZONAL."
  }
}

variable "database_disk_size_gb" {
  description = "Initial SSD capacity in GiB; Cloud SQL automatically expands it as needed"
  type        = number
  default     = 20
  validation {
    condition     = var.database_disk_size_gb >= 10 && floor(var.database_disk_size_gb) == var.database_disk_size_gb
    error_message = "database_disk_size_gb must be an integer of at least 10."
  }
}

variable "database_deletion_protection" {
  description = "Protect the database from deletion through Terraform and the Cloud SQL API"
  type        = bool
  default     = true
}

locals {
  database_name     = "mlr_genie"
  database_iam_user = trimsuffix(google_service_account.mlr_genie_runtime_sa.email, ".gserviceaccount.com")
  # The IAM username and Unix socket are identifiers, not credentials. Access
  # tokens are obtained through ADC by the application at connection time.
  database_url = "postgresql://${urlencode(local.database_iam_user)}@/${local.database_name}?host=${urlencode("/cloudsql/${google_sql_database_instance.mlr_genie.connection_name}")}"
}

resource "google_project_service" "cloud_sql_admin" {
  project            = var.project_id
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

resource "google_sql_database_instance" "mlr_genie" {
  name                = "mlr-genie-postgres"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.database_deletion_protection
  depends_on          = [google_project_service.cloud_sql_admin]

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.database_tier
    availability_type           = var.database_availability_type
    disk_type                   = "PD_SSD"
    disk_size                   = var.database_disk_size_gb
    disk_autoresize             = true
    deletion_protection_enabled = var.database_deletion_protection
    connector_enforcement       = "REQUIRED"

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    # Cloud Run's managed Cloud SQL Auth Proxy uses the instance's public IP.
    # There are no authorized networks; direct database connections are blocked
    # by connector enforcement and access still requires Cloud SQL IAM.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }
  }
}

resource "google_sql_database" "mlr_genie" {
  name            = local.database_name
  instance        = google_sql_database_instance.mlr_genie.name
  deletion_policy = var.database_deletion_protection ? "ABANDON" : "DELETE"
}

resource "google_sql_user" "runtime_iam" {
  name            = local.database_iam_user
  instance        = google_sql_database_instance.mlr_genie.name
  type            = "CLOUD_IAM_SERVICE_ACCOUNT"
  deletion_policy = var.database_deletion_protection ? "ABANDON" : null
}

resource "google_project_iam_member" "runtime_cloud_sql" {
  for_each = toset(["roles/cloudsql.client", "roles/cloudsql.instanceUser"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.mlr_genie_runtime_sa.email}"
}

resource "google_storage_bucket_iam_member" "runtime_artifact_objects" {
  bucket = google_storage_bucket.mlr_artifacts_bucket.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.mlr_genie_runtime_sa.email}"
}

output "database_connection_name" {
  description = "Cloud SQL Auth Proxy and Cloud Run connection name"
  value       = google_sql_database_instance.mlr_genie.connection_name
}

output "database_name" {
  description = "MLR Genie owns this logical database; consumer applications call the API"
  value       = google_sql_database.mlr_genie.name
}

output "database_iam_role" {
  description = "Pass this PostgreSQL role as psql -v runtime_role during schema bootstrap"
  value       = google_sql_user.runtime_iam.name
}

output "artifacts_bucket_name" {
  description = "Private versioned bucket used by the GCS artifact backend"
  value       = google_storage_bucket.mlr_artifacts_bucket.name
}
