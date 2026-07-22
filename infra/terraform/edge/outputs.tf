output "domain_name" {
  value = cloudflare_dns_record.public.name
}

output "target_domain_name" {
  value = cloudflare_dns_record.public.content
}
