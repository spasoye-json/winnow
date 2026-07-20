from googleapiclient.discovery import build

SUBSCRIPTIONS_PAGE_SIZE = 50
PLAYLIST_PAGE_SIZE = 50


def build_client(credentials):
    return build("youtube", "v3", credentials=credentials)


def uploads_playlist_id(channel_id):
    return "UU" + channel_id[2:]


def first_uploads_page(client, playlist_id):
    request = client.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=playlist_id,
        maxResults=PLAYLIST_PAGE_SIZE,
    )
    return request.execute().get("items", [])


def fetch_videos(client, video_ids):
    if not video_ids:
        return []
    response = (
        client.videos()
        .list(part="snippet,contentDetails,statistics", id=",".join(video_ids))
        .execute()
    )
    return response.get("items", [])


def iter_subscriptions(client):
    subscriptions = client.subscriptions()
    request = subscriptions.list(
        part="snippet", mine=True, maxResults=SUBSCRIPTIONS_PAGE_SIZE
    )
    while request is not None:
        response = request.execute()
        yield from response.get("items", [])
        request = subscriptions.list_next(request, response)
