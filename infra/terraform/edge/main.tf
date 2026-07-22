resource "cloudflare_dns_record" "public" {
  zone_id = var.cloudflare_zone_id
  name    = var.domain_name
  content = var.target_domain_name
  type    = "CNAME"
  ttl     = 1
  proxied = false
  comment = "Hindsight stable CloudFront alias"

  lifecycle {
    prevent_destroy = true
  }
}
