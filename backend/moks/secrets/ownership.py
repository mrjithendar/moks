"""Who manages a secret, judged from metadata only, and whether MOKS may offer it for sync at all.

A secret another controller writes (External Secrets, the Secrets Store CSI driver, cert-manager, Helm, Argo CD,
Flux, Sealed Secrets, an AWS service or a rotation function) would have a MOKS write reverted or would fight it.
Such a secret can still be a source. Writing into it needs a mapping whose management policy is "override" and
whose expected owner names that manager, so the operator has said in advance who they are overriding.

Some secrets are never offered: service-account and bootstrap tokens, which Kubernetes issues itself, and Helm's
release records, which are Helm's internal state rather than application secrets.
"""
from __future__ import annotations

from typing import Any

MOKS_LABEL = "moks.dev/secret-sync"
MAPPING_ANNOTATION = "moks.dev/secret-sync-mapping"
AWS_VERSION_ANNOTATION = "moks.dev/aws-version-id"
MOKS_TAG = "moks:managed-by"
MAPPING_TAG = "moks:mapping"

EXCLUDED_TYPES = {
    "kubernetes.io/service-account-token": "service-account token issued by Kubernetes",
    "bootstrap.kubernetes.io/token": "bootstrap token issued by Kubernetes",
    "helm.sh/release.v1": "Helm release record (Helm's own state)",
}
OWNER_KINDS = {"ExternalSecret": "external-secrets", "PushSecret": "external-secrets", "SealedSecret": "sealed-secrets",
               "Certificate": "cert-manager", "CertificateRequest": "cert-manager", "SecretProviderClassPodStatus": "secrets-store-csi"}


def kubernetes_manager(meta: dict[str, Any], type_: str | None) -> dict[str, Any]:
    """{manager, detail, syncable, excluded}. manager None: nothing claims it. 'moks': MOKS created it."""
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    if type_ in EXCLUDED_TYPES:
        return {"manager": "kubernetes", "detail": EXCLUDED_TYPES[type_], "syncable": False, "excluded": EXCLUDED_TYPES[type_]}
    for ref in meta.get("ownerReferences") or meta.get("owner_references") or []:
        kind = ref.get("kind")
        if kind in OWNER_KINDS:
            return _managed(OWNER_KINDS[kind], f"owned by {kind} {ref.get('name')}")
    if labels.get("secrets-store.csi.k8s.io/managed") == "true":
        return _managed("secrets-store-csi", "synced by the Secrets Store CSI driver")
    if "cert-manager.io/certificate-name" in annotations:
        return _managed("cert-manager", f"issued for Certificate {annotations['cert-manager.io/certificate-name']}")
    if labels.get("reconcile.external-secrets.io/managed") == "true" or "reconcile.external-secrets.io/data-hash" in annotations:
        return _managed("external-secrets", "written by External Secrets")
    if labels.get("app.kubernetes.io/managed-by") == "Helm" or "meta.helm.sh/release-name" in annotations:
        return _managed("helm", f"rendered by Helm release {annotations.get('meta.helm.sh/release-name', '?')}")
    if "argocd.argoproj.io/tracking-id" in annotations or "argocd.argoproj.io/instance" in labels:
        return _managed("argocd", "tracked by an Argo CD Application")
    if any(k.startswith(("kustomize.toolkit.fluxcd.io/", "helm.toolkit.fluxcd.io/")) for k in labels):
        return _managed("flux", "applied by Flux")
    if labels.get(MOKS_LABEL) == "true":
        return {"manager": "moks", "detail": f"created by MOKS for mapping {annotations.get(MAPPING_ANNOTATION, '?')}", "syncable": True, "excluded": None}
    return {"manager": None, "detail": "", "syncable": True, "excluded": None}


def aws_manager(meta: dict[str, Any]) -> dict[str, Any]:
    tags = meta.get("tags") or {}
    if meta.get("owning_service"):
        return _managed(f"aws:{meta['owning_service']}", f"managed by the AWS service {meta['owning_service']}")
    if meta.get("rotation_enabled"):
        return _managed("aws-rotation", "rotated by a Secrets Manager rotation function")
    if tags.get(MOKS_TAG) == "moks":
        return {"manager": "moks", "detail": f"created by MOKS for mapping {tags.get(MAPPING_TAG, '?')}", "syncable": True, "excluded": None}
    other = tags.get("managed-by") or tags.get("ManagedBy")
    if other:
        return _managed(str(other).lower(), f"tagged managed-by={other}")
    return {"manager": None, "detail": "", "syncable": True, "excluded": None}


def _managed(manager: str, detail: str) -> dict[str, Any]:
    return {"manager": manager, "detail": detail, "syncable": True, "excluded": None}


def write_refusal(found: str | None, expected: str | None, policy: str) -> str | None:
    """Why MOKS may not write into a secret whose manager is `found`, or None when it may.
    expected: what the mapping says owns it ('none' or a manager name)."""
    if found in (None, "moks"):
        if expected not in (None, "", "none", "moks"):
            return f"the mapping expects {expected} to own this secret, but nothing claims it now; check the mapping"
        return None
    if policy != "override":
        return f"{found} manages this secret; MOKS writes into it only with management policy 'override' and expected owner '{found}'"
    if expected != found:
        return f"{found} manages this secret, but the mapping expects {expected or 'none'}; update the mapping to override {found} explicitly"
    return None
