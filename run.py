import argparse
import fnmatch
import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import shutil
import requests
from pathvalidate import sanitize_filename
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(funcName)20s()][%(levelname)-8s]: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("GoFile")


API_TOKEN = "PASTE_YOUR_API_TOKEN_HERE"  # 🔴 ADD THIS


class File:
    def __init__(self, link: str, dest: str):
        self.link = link
        self.dest = dest


class Downloader:
    def __init__(self, token):
        self.token = token
        self.progress_bar = None
        self.progress_lock = Lock()

    def _get_total_size(self, link):
        r = requests.get(link, headers=self._headers(), stream=True, allow_redirects=True)
        r.raise_for_status()
        size = int(r.headers.get("Content-Length", 0))
        support_range = "bytes" in r.headers.get("Accept-Ranges", "")
        return size, support_range

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Connection": "keep-alive"
        }

    def _download_range(self, link, start, end, temp_file, i):
        existing = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
        range_start = start + existing
        if range_start > end:
            return i

        headers = self._headers()
        headers["Range"] = f"bytes={range_start}-{end}"

        with requests.get(link, headers=headers, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            with open(temp_file, "ab") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        with self.progress_lock:
                            self.progress_bar.update(len(chunk))
        return i

    def _merge_parts(self, temp_dir, dest, threads):
        with open(dest, "wb") as out:
            for i in range(threads):
                path = os.path.join(temp_dir, f"part_{i}")
                with open(path, "rb") as f:
                    out.write(f.read())
                os.remove(path)
        shutil.rmtree(temp_dir)

    def download(self, file: File, num_threads=1):
        link = file.link
        dest = file.dest
        temp_dir = dest + "_parts"

        try:
            total, support_range = self._get_total_size(link)

            if os.path.exists(dest) and os.path.getsize(dest) == total:
                return

            if num_threads == 1 or not support_range:
                temp_file = dest + ".part"
                downloaded = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0

                self.progress_bar = tqdm(
                    total=total,
                    initial=downloaded,
                    unit='B',
                    unit_scale=True,
                    desc=f"Downloading {os.path.basename(dest)[:30]}"
                )

                headers = self._headers()
                headers["Range"] = f"bytes={downloaded}-"

                os.makedirs(os.path.dirname(dest), exist_ok=True)

                with requests.get(link, headers=headers, stream=True, allow_redirects=True) as r:
                    r.raise_for_status()
                    with open(temp_file, "ab") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                self.progress_bar.update(len(chunk))

                self.progress_bar.close()
                os.rename(temp_file, dest)

            else:
                os.makedirs(temp_dir, exist_ok=True)

                part_size = math.ceil(total / num_threads)
                downloaded = 0

                for i in range(num_threads):
                    part_file = os.path.join(temp_dir, f"part_{i}")
                    if os.path.exists(part_file):
                        downloaded += os.path.getsize(part_file)

                self.progress_bar = tqdm(
                    total=total,
                    initial=downloaded,
                    unit='B',
                    unit_scale=True,
                    desc=f"Downloading {os.path.basename(dest)[:30]}"
                )

                futures = []
                with ThreadPoolExecutor(max_workers=num_threads) as ex:
                    for i in range(num_threads):
                        start = i * part_size
                        end = min(start + part_size - 1, total - 1)
                        part_file = os.path.join(temp_dir, f"part_{i}")
                        futures.append(ex.submit(self._download_range, link, start, end, part_file, i))
                    for f in as_completed(futures):
                        f.result()

                self.progress_bar.close()
                self._merge_parts(temp_dir, dest, num_threads)

        except Exception as e:
            if self.progress_bar:
                self.progress_bar.close()
            logger.error(f"Failed to download: {dest} ({e})")


class GoFile:
    API = "https://api.gofile.io"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {API_TOKEN}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

    def get_files_api(self, content_id, password=None):
        url = f"{self.API}/contents/{content_id}"

        params = {}
        if password:
            params["password"] = hashlib.sha256(password.encode()).hexdigest()

        r = self.session.get(url, params=params)
        data = r.json()

        if data.get("status") != "ok":
            raise Exception(f"API error: {data}")

        return data["data"]

    def walk(self, data, base_dir, includes, excludes):
        out = []

        if data["type"] == "file":
            name = sanitize_filename(data["name"])
            if self.include_file(name, includes, excludes):
                out.append(File(data["link"], os.path.join(base_dir, name)))
            return out

        folder = os.path.join(base_dir, sanitize_filename(data["name"]))
        for child in data["children"].values():
            out.extend(self.walk(child, folder, includes, excludes))
        return out

    def include_file(self, name, includes, excludes):
        if includes and not any(fnmatch.fnmatch(name, p) for p in includes):
            return False
        if excludes and any(fnmatch.fnmatch(name, p) for p in excludes):
            return False
        return True

    def execute(self, dir, url=None, content_id=None, password=None,
                proxy=None, num_threads=1, includes=None, excludes=None):

        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        if url:
            content_id = url.rstrip("/").split("/")[-1]

        data = self.get_files_api(content_id, password)
        files = self.walk(data, dir, includes or [], excludes or [])

        for f in files:
            Downloader(API_TOKEN).download(f, num_threads)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("url", nargs='?', default=None)
    group.add_argument("-f", type=str, dest="file")

    parser.add_argument("-t", type=int, dest="num_threads")
    parser.add_argument("-d", type=str, dest="dir")
    parser.add_argument("-p", type=str, dest="password")
    parser.add_argument("-x", type=str, dest="proxy")
    parser.add_argument("-i", action="append", dest="includes")
    parser.add_argument("-e", action="append", dest="excludes")

    args = parser.parse_args()
    dir = args.dir or "./output"
    threads = args.num_threads or 1

    if args.file:
        with open(args.file) as f:
            for line in f:
                url = line.strip()
                if url:
                    GoFile().execute(dir, url=url, password=args.password,
                                     proxy=args.proxy, num_threads=threads,
                                     includes=args.includes, excludes=args.excludes)
    else:
        GoFile().execute(dir, url=args.url, password=args.password,
                         proxy=args.proxy, num_threads=threads,
                         includes=args.includes, excludes=args.excludes)
