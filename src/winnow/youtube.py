from googleapiclient.discovery import build

SUBSCRIPTIONS_PAGE_SIZE = 50


def build_client(credentials):
    return build("youtube", "v3", credentials=credentials)


def iter_subscriptions(client):
    subscriptions = client.subscriptions()
    request = subscriptions.list(
        part="snippet", mine=True, maxResults=SUBSCRIPTIONS_PAGE_SIZE
    )
    while request is not None:
        response = request.execute()
        yield from response.get("items", [])
        request = subscriptions.list_next(request, response)
