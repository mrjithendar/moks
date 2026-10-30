# EKS access for MOKS

When MOKS runs **inside** the EKS cluster, the chart's ServiceAccount and `moks-reader` ClusterRole
are enough. When MOKS runs **outside** (for example on mk4 managing EKS), map an IAM role to a
Kubernetes group with an EKS access entry and bind that group to `moks-reader` (read-only):

```sh
aws eks create-access-entry --cluster-name prod --principal-arn arn:aws:iam::<account>:role/moks \
  --kubernetes-groups moks-readers
kubectl apply -f - <<'YAML'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: moks-readers}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: moks-reader}
subjects: [{kind: Group, name: moks-readers, apiGroup: rbac.authorization.k8s.io}]
YAML
```

Never attach `AmazonEKSClusterAdminPolicy` to the MOKS role. The remote-EKS connector (native
STS token generation) is Phase 2; until then MOKS can only reach EKS from inside it.
