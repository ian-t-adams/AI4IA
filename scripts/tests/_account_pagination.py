"""Shared invalid account-continuation fixtures for both CLI consumers."""

from urllib.parse import parse_qs, urlencode, urlsplit


def invalid_account_continuations(
    good: str, subscription: str, other_subscription: str, resource_group: str,
) -> list[str]:
    parsed = urlsplit(good)
    path = parsed.path
    base = f"{parsed.scheme}://{parsed.netloc}"
    parameters = {key: values[0] for key, values in parse_qs(parsed.query).items()}

    def link(**overrides: str) -> str:
        return base + path + "?" + urlencode({**parameters, **overrides}, safe="$")

    return [
        good.replace("https:", "http:", 1),
        good.replace("management.azure.com", "management.azure.com.evil.invalid", 1),
        good.replace("management.azure.com", "user:password@management.azure.com", 1),
        good.replace("management.azure.com", "management.azure.com:443", 1),
        good.replace(subscription, other_subscription),
        good.replace(resource_group, "other-group"),
        good.replace(path, path + "/deployments"),
        good.replace(path, path + "/../accounts"),
        good.replace(path, path + "/"),
        good.replace(path, path.replace("/resourceGroups/", "/resourceGroups%2f")),
        good.removeprefix(base),
        good + "#fragment",
        good + "#",
        " " + good,
        good + "\n",
        link(**{"api-version": "2023-05-01"}),
        link(**{"$filter": "kind eq 'AIServices'"}),
        good + "&api-version=" + parameters["api-version"],
        good + "&$skiptoken=duplicate",
        base + path + "?" + urlencode({"api-version": parameters["api-version"], "$skipToken": parameters["$skiptoken"]}),
        base + path + "?" + urlencode({"$skiptoken": parameters["$skiptoken"]}),
        base + path + "?api-version=" + parameters["api-version"],
    ]
