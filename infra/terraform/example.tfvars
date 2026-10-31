# Example values only: placeholders, no secrets. Copy to <environment>.tfvars (ignored by
# git) and fill in real ones once the user has approved an account. Secrets never go here.
region              = "us-east-1"
environment         = "production"
acm_certificate_arn = "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
image_tag           = "REPLACE-with-a-release-tag" # fails validation until replaced
api_count           = 2
worker_count        = 2
# market_data_provider = ""   # stays empty until the user chooses a vendor and licence (#11)
# llm_enabled          = false
