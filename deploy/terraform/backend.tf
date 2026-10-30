terraform {
  # The deployment driver supplies the private, versioned state bucket and prefix.
  backend "gcs" {}
}
