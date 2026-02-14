### Song downloader specifications

# One-line summary
A Python program that downloads missing songs.

# Input
A destination folder will be provided, already containing some songs.
The name of a file within that folder will also be provided, containing a list of all the songs currently in the playlist, each line containing playlist index, video id, and song title, in that order, separated by triple semicolons.

# Logic
The program should check through all the song files currently in the folder, compare them to the full list, and download any that are missing.

# Song download format requirements
- `yt-dlp` is the program which should be used to download songs from Youtube. 
- The original command being used was: `yt-dlp -x -o "%(playlist_autonumber)s - %(title)s.%(ext)s" https://www.youtube.com/playlist?list=[playlist_id]`.
- This has the following effects:
- - `-x` downloads only the audio of a given Youtube video.
- - `-o` specifies the output file name format. Filenames should have the playlist index, the song name and the correct extension.
- The downloading functionality should mimic this in end result, but **must** download the songs, individually, without referencing the web playlist in question. Details about eg. playlist index should be pulled from the file given.

# Downloading caveats
Youtube places rate restrictions on downloads. The program should therefore space out download requests. Research should be done on how many songs can be downloaded in a given duration.

# Miscellaneous
The current project should be contained within the `downloader_fixer` folder. 
In a previous session, the `playlist_fixer.py` program was created to help rename song files with incorrect file names. Several rounds of iteration were required. Please read that program and copy implementation details with respect to specific character encodings, valid Windows file name characters, and so on.
