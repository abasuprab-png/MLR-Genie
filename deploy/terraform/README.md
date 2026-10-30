# External HTTPS entry point

The global external Application Load Balancer forwards HTTPS through a regional
serverless NEG to Cloud Run. Keep `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`:
Azure callers use the public load balancer, while direct Internet access to the
Cloud Run URL remains blocked. `allUsers` receives Cloud Run invoker permission
because the load balancer forwards MLR bearer tokens rather than Google identity
tokens. Hosted application authentication remains mandatory.

Cloud Armor throttles `/auth/*` at 60 requests per minute per source IP and
`/integrations/*` at 600. Excess requests receive 429 before application work.
These limits aggregate callers behind the same Azure outbound IP; assess their
capacity before rollout. Cloud Armor limits are approximate traffic controls,
not a token-issuance authorization mechanism.

Validate without deploying:

```sh
terraform init -backend=false
terraform fmt -check
terraform validate
```

Deployment is a separate authorized step. Before it, supply `project_id`,
`region`, an immutable `app_image` digest, and a controlled DNS `public_host`
(for example, `mlr.example.org`). Enable the Compute Engine, Cloud Run, Secret
Manager, and required image/provider APIs in that project. The deployment actor
needs the corresponding resource-management and service-account permissions;
an organization policy that forbids `allUsers` invocation must be resolved for
this application-authenticated architecture before rollout. Populate the
existing signing, bootstrap, and Veeva Secret Manager versions outside Terraform
before starting the revision. No secret values belong in plans or state.

After an authorized apply, create the `public_host` DNS A record using
`terraform output -raw public_ip`. Remove conflicting AAAA records unless an
IPv6 frontend is separately configured. Wait for the Google-managed certificate
to become `ACTIVE`; Terraform resource creation alone does not establish HTTPS
readiness. The service URL is `terraform output -raw public_url`. Configure CAMS
on Azure and CommsRx with that HTTPS URL, without a path prefix.

From outside Google Cloud, verify `/health` returns 200 and a tokenless multipart
POST to `/integrations/cams/verify-source` returns 401. Then run the hosted live
integration evaluator through the public URL. Record the host, deployed image
digest, and a successful requester verification using a real journal PDF with a
reworded claim in the authorized deployment's evidence. A local hosted-mode test
does not prove public DNS, certificate, load-balancer, or Azure connectivity.
