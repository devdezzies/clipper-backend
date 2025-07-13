from pytubefix import YouTube
from pytubefix.cli import on_progress

url_1 = 'https://www.youtube.com/watch?v=B3e3c-HYw-U'
url_2 = 'https://www.youtube.com/watch?v=_A0z0ymj15s'

yt = YouTube(url_2, on_progress_callback=on_progress)
print(f'Video title {yt.title}')

ys = yt.streams.get_highest_resolution()
ys.download()