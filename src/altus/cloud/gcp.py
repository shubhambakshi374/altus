"""GCP: method classification and the discovery session.

Google ships its API contracts *on disk*. ``google-api-python-client`` bundles
600 discovery documents covering 335 APIs and 26,438 methods, so introspection
costs no network call and the classification tests sweep a real corpus --- the
same position botocore puts AWS in, and the one Azure could not be in because
its operation catalog is a network call.

Those documents also state each method's HTTP verb, which is what lets this
classifier parse where the AWS one has to read English. Of 10,987 GET methods
exactly five have a write-shaped name, and they are listed below by hand.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from altus.cloud.base import CloudTarget, Sensitivity

#: Results returned from one call. Paginators will walk a whole project given
#: the chance, and the model pays for every row.
MAX_RESULTS = 500

# --------------------------------------------------------------- the corpus


@lru_cache(maxsize=1)
def _documents_dir() -> str:
    import googleapiclient

    return os.path.join(os.path.dirname(googleapiclient.__file__), "discovery_cache", "documents")


@lru_cache(maxsize=1)
def _catalog() -> dict[str, tuple[str, ...]]:
    """API name -> the versions shipped, best first.

    One directory listing, cached. "Best" prefers a stable version over a
    preview one: an alpha document can describe a method that no longer exists,
    and classifying against it would answer about the wrong API.
    """
    found: dict[str, list[str]] = {}
    try:
        names = os.listdir(_documents_dir())
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        api, _, version = name[: -len(".json")].partition(".")
        if api and version:
            found.setdefault(api, []).append(version)
    return {api: tuple(sorted(versions, key=_version_rank)) for api, versions in found.items()}


def _version_rank(version: str) -> tuple[int, str]:
    preview = 1 if ("alpha" in version or "beta" in version) else 0
    return (preview, version)


def available_apis() -> list[str]:
    return sorted(_catalog())


@lru_cache(maxsize=128)
def discovery_document(api: str, version: str = "") -> dict[str, Any]:
    """One shipped document, parsed. Raises LookupError when there is none."""
    versions = _catalog().get(api)
    if not versions:
        raise LookupError(f"no discovery document ships for {api!r}")
    chosen = version or versions[0]
    if chosen not in versions:
        raise LookupError(f"{api} has no version {chosen!r}; shipped: {', '.join(versions)}")
    path = os.path.join(_documents_dir(), f"{api}.{chosen}.json")
    with open(path, encoding="utf-8") as handle:
        return dict(json.load(handle))


@lru_cache(maxsize=128)
def method_index(api: str, version: str = "") -> dict[str, dict[str, Any]]:
    """Every method in one document, keyed by its dotted id.

    Resources nest arbitrarily --- ``iam.projects.serviceAccounts.keys.create``
    is four levels down --- so this flattens once and caches, rather than
    walking the tree per lookup.
    """
    document = discovery_document(api, version)
    found: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for name, method in (node.get("methods") or {}).items():
            found[str(method.get("id") or f"{prefix}.{name}")] = dict(method)
        for name, child in (node.get("resources") or {}).items():
            walk(child, f"{prefix}.{name}")

    walk(document, api)
    return found


def lookup(method_id: str) -> dict[str, Any] | None:
    """A method's discovery entry, across every shipped version of its API.

    None means the corpus does not know it, which ``classify`` treats as the
    strictest case rather than guessing --- it means a typo, or an API newer
    than the pinned client.
    """
    api = str(method_id).split(".", 1)[0]
    for version in _catalog().get(api, ()):
        try:
            found = method_index(api, version).get(method_id)
        except OSError, ValueError, LookupError:
            continue
        if found is not None:
            return found
    return None


def methods(api: str, version: str = "") -> list[str]:
    return sorted(method_index(api, version))


# ---------------------------------------------------------- classification

#: The five GET methods whose names say they change something. Measured across
#: all 10,987 GET methods in the corpus, and hand-listed because five is small
#: enough to be exact and a pattern would catch innocent names too.
GET_BUT_WRITES: frozenset[str] = frozenset(
    {
        "beyondcorp.organizations.locations.subscriptions.cancel",
        "developerconnect.projects.locations.accountConnectors.users.startOAuthFlow",
        "firebaseappdistribution.projects.apps.releases.tests.cancel",
        "datalabeling.projects.operations.cancel",
        "adsensehost.associationsessions.start",
    }
)

#: Reads that are not GET. 1,080 of the 1,108 in the corpus are the first two.
POST_READ_VERBS: frozenset[str] = frozenset(
    {
        "getIamPolicy",
        "testIamPermissions",
        "list",
        "search",
        "searchAll",
        "searchAllResources",
        "searchAllIamPolicies",
        "query",
        "batchGet",
        "lookup",
        "aggregatedList",
        "fetch",
        "check",
        "analyze",
        "estimate",
        "simulate",
    }
)

#: Methods that mint access outliving the conversation. Exact ids: each one is
#: a specific, known escalation rather than a shape to match.
CREDENTIAL_MINTING: frozenset[str] = frozenset(
    {
        "iam.projects.serviceAccounts.keys.create",
        "iamcredentials.projects.serviceAccounts.generateAccessToken",
        "iamcredentials.projects.serviceAccounts.generateIdToken",
        "iamcredentials.projects.serviceAccounts.signBlob",
        "iamcredentials.projects.serviceAccounts.signJwt",
    }
)

#: APIs whose *writes* decide who may do what, or hold key material. Reading
#: them is ordinary --- listing service accounts is how you understand a
#: project --- but changing one is the GCP equivalent of editing a
#: ClusterRoleBinding.
PRIVILEGED_APIS: frozenset[str] = frozenset(
    {
        "iam",
        "iamcredentials",
        "cloudresourcemanager",
        "cloudkms",
        "secretmanager",
        "accesscontextmanager",
        "orgpolicy",
        "admin",
        "cloudidentity",
        "essentialcontacts",
        "policysimulator",
        "privateca",
    }
)

#: Reads that hand back secret material.
SECRET_READS: frozenset[str] = frozenset(
    {
        "secretmanager.projects.secrets.versions.access",
        "secretmanager.projects.locations.secrets.versions.access",
        # Returns masterAuth --- the cluster CA certificate and, on older
        # clusters, a username and password. The same argument that put
        # eks:DescribeCluster in the AWS sensitive set.
        "container.projects.locations.clusters.get",
        "container.projects.zones.clusters.get",
    }
)
SECRET_READ_PATTERN = re.compile(
    r"\.(getCredentials|fetchAccessToken|generateConfig|getSecret)$", re.IGNORECASE
)

#: A single call here can put a private workload on the internet.
EXPOSURE_METHODS: frozenset[str] = frozenset(
    {
        "compute.firewalls.insert",
        "compute.firewalls.patch",
        "compute.firewalls.update",
        "compute.instances.addAccessConfig",
        "compute.instances.updateAccessConfig",
        "compute.globalForwardingRules.insert",
        "compute.forwardingRules.insert",
        "compute.targetPools.insert",
        "compute.targetPools.addInstance",
        "compute.networks.insert",
        "compute.routes.insert",
        "compute.securityPolicies.patch",
        "compute.securityPolicies.removeRule",
        "storage.buckets.update",
    }
)

#: Verbs that destroy rather than change.
DESTROY_VERBS: frozenset[str] = frozenset(
    {"delete", "batchDelete", "destroy", "purge", "drop", "truncate", "remove", "undelete"}
)

#: Collections where deletion is unrecoverable rather than merely inconvenient
#: --- the object holds data, or everything inside it goes with it.
#:
#: Matched against the *last* path segment only, which matters more than it
#: looks. Nearly every GCP method id contains `projects` and many contain
#: `locations` or `jobs`, so matching the whole path made 1,332 of 1,809
#: deletes privileged --- including `…operations.delete`, which removes a
#: bookkeeping record. That is the AWS bug repeated: when every delete demands
#: the name typed out, people learn to type through it and the gate stops
#: working.
STATEFUL_COLLECTIONS = re.compile(
    r"^(buckets|objects|instances|disks|snapshots|images|databases|tables|datasets"
    r"|datasetVersions|clusters|nodePools|projects|folders|organizations|secrets"
    r"|keys|cryptoKeys|keyRings|repositories|registries|topics|subscriptions"
    r"|backups|volumes|shares|domains|managedZones|addresses|networks|subnetworks)$",
    re.IGNORECASE,
)


def scope_level(scope: str) -> str:
    """How wide a scope path is.

    GCP's hierarchy is organisation → folder → project, and blast radius is a
    function of where a call lands as well as what it is --- the same delete
    removes one instance in a project and every project under a folder.
    """
    lowered = str(scope or "").casefold().strip("/")
    if not lowered:
        return "unknown"
    if lowered.startswith("organizations/"):
        return "organization"
    if lowered.startswith("folders/"):
        return "folder"
    if lowered.startswith("projects/") or lowered.startswith("//"):
        return "project"
    return "unknown"


#: Scopes at which a change is not about one object.
WIDE_SCOPES: frozenset[str] = frozenset({"organization", "folder"})


def classify(method_id: str, scope: str = "") -> Sensitivity:
    """Sensitivity of one method at one scope.

    Precedence runs highest-first, because a method is often several things at
    once and the most dangerous reading is the one that should win:
    ``cloudresourcemanager.projects.delete`` is both an identity-service write
    and the destruction of everything in it.

    A method id the corpus does not know fails closed to PRIVILEGED. That means
    a typo or an API newer than the pinned client, which is precisely where
    guessing low is unrecoverable.
    """
    found = lookup(method_id)
    if found is None:
        return Sensitivity.PRIVILEGED

    api = method_id.split(".", 1)[0]
    verb = method_id.rsplit(".", 1)[-1]
    collection = method_id[len(api) + 1 : -(len(verb) + 1)] if "." in method_id else ""
    reads = _is_read(method_id, verb, str(found.get("httpMethod", "POST")))

    # setIamPolicy is the single most consequential call in GCP: it is how a
    # bucket becomes world-readable and how anyone grants themselves owner.
    # 478 of them, across every service.
    if verb == "setIamPolicy":
        return Sensitivity.PRIVILEGED
    if method_id in CREDENTIAL_MINTING:
        return Sensitivity.PRIVILEGED
    if not reads and api in PRIVILEGED_APIS:
        return Sensitivity.PRIVILEGED
    if not reads and scope_level(scope) in WIDE_SCOPES:
        return Sensitivity.PRIVILEGED
    if not reads and method_id in EXPOSURE_METHODS:
        return Sensitivity.PRIVILEGED
    if verb in DESTROY_VERBS and STATEFUL_COLLECTIONS.match(collection.rsplit(".", 1)[-1]):
        return Sensitivity.PRIVILEGED

    if reads:
        if method_id in SECRET_READS or SECRET_READ_PATTERN.search(method_id):
            return Sensitivity.SENSITIVE_READ
        return Sensitivity.READ
    return Sensitivity.MUTATE


def _is_read(method_id: str, verb: str, http_method: str) -> bool:
    """GET means read, with five hand-listed exceptions; plus the POST reads.

    Structural rather than inferred, which is what makes this classifier more
    trustworthy than the AWS one --- there is no "unknown verb" branch for a
    method the corpus knows, and with 2,369 distinct verbs (1,087 of them
    appearing exactly once) a verb allowlist would never have worked.
    """
    if http_method.upper() == "GET":
        return method_id not in GET_BUT_WRITES
    return verb in POST_READ_VERBS


def supports_validate_only(method_id: str) -> str:
    """The name of this method's dry-run parameter, or "" when it has none.

    Asked of the discovery document rather than assumed: only 498 of 26,438
    methods take one --- 1.9%, the weakest preview of the four clouds --- and
    guessing wrong means either a skipped preflight or a parameter error.
    """
    found = lookup(method_id)
    if found is None:
        return ""
    for name in found.get("parameters") or {}:
        if str(name).casefold() in {"validateonly", "validate_only", "dryrun", "dry_run"}:
            return str(name)
    return ""


def describe_method(method_id: str) -> dict[str, Any]:
    """The parameter contract, straight from the shipped document.

    What makes the API path easier than guessing `gcloud` flags, and the
    cheapest of the four clouds' equivalents because it needs no network at all.
    """
    found = lookup(method_id)
    if found is None:
        raise LookupError(f"no method {method_id!r} in any shipped discovery document")
    parameters: dict[str, Any] = {}
    required = set(found.get("parameterOrder") or [])
    for name, spec in (found.get("parameters") or {}).items():
        parameters[name] = {
            "type": str(spec.get("type", "")),
            "required": bool(spec.get("required")) or name in required,
            "location": str(spec.get("location", "")),
            "documentation": _flatten(str(spec.get("description", "")))[:300],
        }
    return {
        "id": method_id,
        "http_method": str(found.get("httpMethod", "")),
        "path": str(found.get("path", "")),
        "sensitivity": classify(method_id).value,
        "validate_only": supports_validate_only(method_id),
        "request_body": bool(found.get("request")),
        "documentation": _flatten(str(found.get("description", "")))[:600],
        "parameters": parameters,
        "scopes": [str(s) for s in found.get("scopes") or []],
    }


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def target_for(project: str, region: str = "", zone: str = "") -> CloudTarget:
    return CloudTarget(
        cloud="gcp", context=project or "unknown-project", location=region, scope=zone
    )


# ------------------------------------------------------------------ the session

#: Keys that are pagination bookkeeping, not answers.
PAGINATION_KEYS = frozenset({"nextPageToken", "pageToken", "etag", "kind", "selfLink"})


@dataclass
class GcpProvider:
    """Credentials and discovery clients, built on first use and cached.

    Mirrors ``AzureProvider`` and ``AwsProvider``: resolving ADC walks the
    filesystem and may hit the metadata server, and building a client parses a
    discovery document, so a session that never mentions GCP should do neither.
    Cached per (api, version), because a client is bound to both.
    """

    project: str = ""
    max_results: int = MAX_RESULTS
    _credentials: Any = field(default=None, repr=False)
    _clients: dict[tuple[str, str], Any] = field(default_factory=dict, repr=False)
    _identity: dict[str, str] | None = field(default=None, repr=False)

    def reset(self) -> None:
        """Drop everything cached. Called when the project changes: a cached
        client carries the credentials it was built with, and reusing one would
        send the next call somewhere the user did not approve."""
        self._credentials = None
        self._clients.clear()
        self._identity = None

    def credentials(self) -> tuple[Any, str]:
        """Application Default Credentials, and the project they name."""
        if self._credentials is None:
            import google.auth

            self._credentials = google.auth.default()
        found, discovered = self._credentials
        return found, str(discovered or "")

    def default_project(self) -> str:
        if self.project:
            return self.project
        try:
            return self.credentials()[1]
        except Exception:
            return ""

    def version_for(self, api: str) -> str:
        versions = _catalog().get(api)
        if not versions:
            raise LookupError(
                f"no discovery document ships for {api!r}. Call gcp_apis to see what does."
            )
        return versions[0]

    def client(self, api: str, version: str = "") -> Any:
        chosen = version or self.version_for(api)
        key = (api, chosen)
        if key not in self._clients:
            from googleapiclient.discovery import build

            credentials, _project = self.credentials()
            self._clients[key] = build(
                api,
                chosen,
                credentials=credentials,
                # The documents are on disk; going to the network for them
                # would make every first call wait on Google's CDN and break
                # entirely offline.
                static_discovery=True,
                cache_discovery=False,
            )
        return self._clients[key]

    def _resolve(self, method_id: str, version: str = "") -> tuple[Any, str]:
        """Walk a dotted id down to the collection that owns the method.

        ``iam.projects.serviceAccounts.keys.create`` is four levels of nested
        resource, each of which is a *call* on the one above it, so this cannot
        be a simple getattr chain.
        """
        api, *rest = method_id.split(".")
        if not rest:
            raise LookupError(f"{method_id!r} is not a method id, e.g. compute.instances.list")
        node = self.client(api, version)
        for part in rest[:-1]:
            accessor = getattr(node, part, None)
            if accessor is None:
                raise LookupError(f"{api} has no collection {part!r} in {method_id}")
            node = accessor()
        return node, rest[-1]

    async def call(
        self,
        method_id: str,
        params: dict[str, Any] | None = None,
        *,
        version: str = "",
        limit: int = 0,
    ) -> dict[str, Any]:
        """One method, paged to a cap.

        Returns the response with a ``_truncated`` marker when the cap was hit.
        Silently returning the first page would have the model reason about a
        partial answer as though it were the whole one --- the same argument
        the AWS and Azure paginators make.
        """
        cap = limit or self.max_results
        arguments = dict(params or {})

        def _run() -> dict[str, Any]:
            node, verb = self._resolve(method_id, version)
            method = getattr(node, verb, None)
            if method is None:
                raise LookupError(f"{method_id} is not a method this client exposes")
            request = method(**arguments)
            response = dict(request.execute())

            follow = getattr(node, f"{verb}_next", None)
            if follow is None:
                return {k: v for k, v in response.items() if k not in PAGINATION_KEYS}

            merged: dict[str, Any] = {}
            counted = 0
            truncated = False
            page: dict[str, Any] | None = response
            while page is not None:
                for key, value in page.items():
                    if key in PAGINATION_KEYS:
                        continue
                    if isinstance(value, list):
                        room = cap - counted
                        merged.setdefault(key, []).extend(value[:room])
                        counted += min(len(value), max(0, room))
                    else:
                        merged.setdefault(key, value)
                if counted >= cap:
                    truncated = page.get("nextPageToken") is not None
                    break
                request = follow(request, page)
                if request is None:
                    break
                page = dict(request.execute())
            if truncated:
                merged["_truncated"] = f"stopped at {cap} results"
            return merged

        return await asyncio.to_thread(_run)

    # -------------------------------------------------------------- identity

    async def whoami(self) -> dict[str, str]:
        """Who ADC says we are, without a network call where possible.

        The credential object already carries the service-account email or the
        authorised user; asking an API for it would cost a round trip on every
        identity panel.
        """
        if self._identity is None:

            def _read() -> dict[str, str]:
                credentials, discovered = self.credentials()
                account = (
                    getattr(credentials, "service_account_email", None)
                    or getattr(credentials, "_account", None)
                    or getattr(credentials, "quota_project_id", None)
                    or ""
                )
                return {
                    "account": str(account),
                    "project": self.project or str(discovered or ""),
                    "source": type(credentials).__name__,
                }

            self._identity = await asyncio.to_thread(_read)
        return self._identity

    async def projects(self) -> list[dict[str, str]]:
        payload = await self.call("cloudresourcemanager.projects.search")
        return [
            {
                "id": str(item.get("projectId", "")),
                "name": str(item.get("displayName") or item.get("name", "")),
                "state": str(item.get("state") or item.get("lifecycleState", "")),
            }
            for item in payload.get("projects") or []
        ]

    # --------------------------------------------------------------- assets

    async def assets(
        self, query: str = "", asset_types: list[str] | None = None, scope: str = ""
    ) -> dict[str, Any]:
        """Cloud Asset Inventory --- GCP's Resource Graph.

        One call across every resource in the project, instead of a walk of
        each service. Needs the API enabled and a permission plenty of
        project-level identities lack, so callers are expected to fall back
        rather than treat a failure as "nothing is there".
        """
        params: dict[str, Any] = {"scope": scope or f"projects/{self.default_project()}"}
        if query:
            params["query"] = query
        if asset_types:
            params["assetTypes"] = asset_types
        return await self.call("cloudasset.resources.searchAll", params)

    # ------------------------------------------------------------- preflight

    async def test_permissions(self, method_id: str, resource: str) -> list[str]:
        """Which of the permissions this method needs we actually hold.

        The GCP twin of k8s_can_i, checkAccess and SimulatePrincipalPolicy ---
        and the most widely available of the four, declared on 598 resources
        and needing no extra permission to ask about yourself.
        """
        permission = permission_for(method_id)
        if not permission:
            return []
        owner = method_id.rsplit(".", 1)[0]
        payload = await self.call(
            f"{owner}.testIamPermissions",
            {"resource": resource, "body": {"permissions": [permission]}},
        )
        return [str(p) for p in payload.get("permissions") or []]


def permission_for(method_id: str) -> str:
    """The IAM permission string a method needs.

    GCP's permissions read almost exactly like its method ids ---
    ``compute.instances.delete`` is both --- so the translation is mostly
    identity, with the middle path segments that are pure routing removed.
    """
    parts = [p for p in method_id.split(".") if p not in {"projects", "locations", "zones"}]
    if len(parts) < 3:
        return method_id
    return f"{parts[0]}.{parts[-2]}.{parts[-1]}"
