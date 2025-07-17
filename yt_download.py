from pytubefix import YouTube
from pytubefix.cli import on_progress

url_1 = 'https://www.youtube.com/watch?v=B3e3c-HYw-U'
url_2 = 'https://www.youtube.com/watch?v=FQmdrv3cO6A'

yt = YouTube(url_2, on_progress_callback=on_progress)
print(f'Video title {yt.title}')

ys = yt.streams.get_highest_resolution()
ys.download()

# ffmpeg -ss 00:15:00 -to 00:30:00 -i obrolan.mp4 -c copy obrolan15min.mp4