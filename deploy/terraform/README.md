# MOKS AWS resources (Terraform)

This module creates everything MOKS needs in AWS account `881026659929` (us-east-1):

| Resource | Purpose |
|---|---|
| ECR repository `moks` | Mutable tags (CI moves `latest`), scan on push, AES-256. Lifecycle: untagged images expire after 1 day; the newest 30 commit images are kept. `force_delete = false` |
| ECR repository policy (optional) | Pull access for listed principals (`ecr_pull_principal_arns`) |
| IAM OIDC provider `token.actions.githubusercontent.com` | Lets GitHub Actions use short-lived credentials. It is account-wide; set `create_github_oidc_provider = false` if another stack manages it |
| IAM role `moks-github-ci` | Assumable only by `master` of this repository, matched on GitHub's immutable subject `repo:mrjithendar@20453233/moks@1439332847:ref:refs/heads/master` (`github_oidc_subject_prefix`); can push to `moks` only |
| IAM user `/moks/moks-mk4-runtime` | Identity for MOKS pods on mk4, created **without an access key** |
| User policy `invoke-selected-bedrock-profile` | `bedrock:InvokeModel` (which also covers Converse) on one inference profile and its model through that profile only |
| User policy `invoke-claude-on-bedrock` (optional) | `bedrock-mantle:CreateInference` in `aws_region`, for current Claude models; off until `bedrock_claude_enabled = true` |
| OpenSearch domain (optional) | Single-node knowledge search that only the runtime user can call, with signed requests; off while `opensearch_domain_name = ""`. **Billed hourly** |
| User policy `read-allowed-log-groups` | Phase 2 CloudWatch Logs Insights on listed log groups; off while `cloudwatch_log_group_arns = []` |

State: `s3://jithendardharmapuri/terraform/moks.tfstate`, the same bucket as
`roboshop-ai/infrastructure` with a separate key, encrypted, with S3 lockfiles. The state holds
no secrets, because no access keys are created here.

## Apply

```sh
cd deploy/terraform
terraform init
terraform plan  -var-file=tfvars/mk4.tfvars -out=moks.tfplan   # expect 7 to add on a fresh account
terraform apply moks.tfplan
terraform output
```

Commit `.terraform.lock.hcl` with the module. `*.tfplan`, state and `.terraform/` are ignored.

## After apply

1. **CI:** store the role ARN as a repository *variable*, not a secret:
   `gh variable set AWS_ROLE_ARN --repo mrjithendar/moks --body "$(terraform output -raw ci_role_arn)"`.
   No `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` secrets are needed.
2. **Runtime key for mk4**, after `scripts/create-secrets.sh` has created `moks-secrets`:
   `scripts/create-runtime-key.sh mk4` (use `ROTATE=1` to rotate later).
3. **Bedrock in MOKS:** go to Administration → LLM providers → Add, choose provider `bedrock`,
   region `us-east-1`, and model `terraform output -raw bedrock_model_for_moks`
   (`us.amazon.nova-2-lite-v1:0`), then Test and Activate. Model access must be enabled for the
   account in the Bedrock console. To use another model, change both `bedrock_inference_profile_id`
   and `bedrock_model_id` and re-apply. For current Claude models, set `bedrock_claude_enabled = true`
   and use a model such as `anthropic.claude-opus-5-5`.

## Image pulls on mk4

Nodes pull with the `ecr-pull` Secret, which mk4's `ecr-refresh` CronJob refreshes using the AWS
keys in `roboshop-ai/roboshop-secrets`. That principal must be allowed to pull `moks`. Find it
without printing the secret key:

```sh
aws iam get-access-key-last-used --access-key-id "$(kubectl --context mk4 -n roboshop-ai get secret roboshop-secrets -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)" --query UserName --output text
```

If that user's policy already allows `ecr:BatchGetImage` on `*` or on `repository/*`, nothing
more is needed. Otherwise, add its ARN to `ecr_pull_principal_arns` in `mk4.tfvars` and apply.

## Not managed here

- The S3 state bucket. Enable versioning on it (`aws s3api put-bucket-versioning --bucket jithendardharmapuri --versioning-configuration Status=Enabled`), since it holds the state of every stack.
- The RoboShop repositories and roles (`roboshop-ai/infrastructure`).
- EKS: use IRSA or Pod Identity with the policies in [../iam/](../iam/) instead of the runtime user.

## Troubleshooting CI sign-in

`Not authorized to perform sts:AssumeRoleWithWebIdentity` means the GitHub token's `sub` claim does
not match the trust policy. Compare `gh api repos/mrjithendar/moks/actions/oidc/customization/sub`
(field `sub_claim_prefix`) with `github_oidc_subject_prefix` in `tfvars/mk4.tfvars`. Workflows that
use a GitHub environment send `:environment:<name>` instead of `:ref:...`.
