# Public Azure/CAMS callers use this entry point. Cloud Run's internal-and-LB
# ingress continues to reject direct Internet requests to the run.app URL.
variable "public_host" {
  description = "DNS hostname for the HTTPS endpoint; point its A record at public_ip after deployment"
  type        = string
  validation {
    condition = (
      length(var.public_host) <= 253 &&
      can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.public_host))
    )
    error_message = "public_host must be a lowercase DNS hostname, without a scheme, path, wildcard, or port."
  }
}

resource "google_compute_global_address" "mlr_genie_https" {
  name = "mlr-genie-https"
}

resource "google_compute_managed_ssl_certificate" "mlr_genie_https" {
  name = "mlr-genie-https"
  managed {
    domains = [var.public_host]
  }
}

resource "google_compute_region_network_endpoint_group" "mlr_genie_serverless" {
  name                  = "mlr-genie-serverless"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = google_cloud_run_v2_service.mlr_genie_service.name
  }
}

# The LB forwards the application's MLR bearer token, not a Google identity
# token. Ingress restricts network reachability; hosted application auth still
# gates protected routes. Do not change ingress to ALL with this IAM grant.
resource "google_cloud_run_v2_service_iam_member" "https_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.mlr_genie_service.location
  name     = google_cloud_run_v2_service.mlr_genie_service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_compute_security_policy" "mlr_genie_https" {
  name        = "mlr-genie-https"
  description = "Per-source-IP throttling before MLR token issuance and integration processing"

  rule {
    action   = "throttle"
    priority = 1000
    match {
      expr {
        expression = "request.path.urlDecode().startsWith('/auth/')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 60
        interval_sec = 60
      }
    }
    description = "Protect bootstrap token issuance before application authentication"
  }

  rule {
    action   = "throttle"
    priority = 1100
    match {
      expr {
        expression = "request.path.urlDecode().startsWith('/integrations/')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 600
        interval_sec = 60
      }
    }
    description = "Limit integration traffic, including unauthenticated requests"
  }

  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Application authentication applies to all other traffic"
  }
}

resource "google_compute_backend_service" "mlr_genie_https" {
  name                  = "mlr-genie-https"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.mlr_genie_https.id

  # Serverless NEGs do not use Compute health checks or backend timeout_sec.
  backend {
    group = google_compute_region_network_endpoint_group.mlr_genie_serverless.id
  }
}

resource "google_compute_url_map" "mlr_genie_https" {
  name            = "mlr-genie-https"
  default_service = google_compute_backend_service.mlr_genie_https.id
}

resource "google_compute_target_https_proxy" "mlr_genie_https" {
  name             = "mlr-genie-https"
  url_map          = google_compute_url_map.mlr_genie_https.id
  ssl_certificates = [google_compute_managed_ssl_certificate.mlr_genie_https.id]
}

resource "google_compute_global_forwarding_rule" "mlr_genie_https" {
  name                  = "mlr-genie-https"
  ip_address            = google_compute_global_address.mlr_genie_https.id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.mlr_genie_https.id
  depends_on            = [google_cloud_run_v2_service_iam_member.https_invoker]
}

output "public_url" {
  description = "CAMS/CommsRx base URL, usable once DNS and the managed certificate are ready"
  value       = "https://${var.public_host}"
}

output "public_ip" {
  description = "Create a public_host DNS A record pointing to this global IPv4 address"
  value       = google_compute_global_address.mlr_genie_https.address
}
