mock_provider "cloudflare" {}

run "stable_edge_record" {
  command = plan

  variables {
    cloudflare_zone_id = "00000000000000000000000000000000"
    domain_name        = "hindsight.example.com"
    target_domain_name = "d0000000000000.cloudfront.net"
  }

  assert {
    condition = (
      cloudflare_dns_record.public.type == "CNAME" &&
      cloudflare_dns_record.public.name == var.domain_name &&
      cloudflare_dns_record.public.content == var.target_domain_name &&
      cloudflare_dns_record.public.proxied == false
    )
    error_message = "The stable edge record must remain a DNS-only CNAME to the selected distribution."
  }
}
