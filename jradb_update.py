import os
import requests
import zipfile
import shutil
import glob
from lxml import etree
import lhafile
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials

# --- 環境変数から認証情報を取得 ---
JRADB_USER = os.environ["JRADB_USER"]
JRADB_PASS = os.environ["JRADB_PASS"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GCP_SA_KEY_PATH = "service_account.json" # Actions側で生成する

# --- 設定 ---
TEMP_DIR = "temp_download"
os.makedirs(TEMP_DIR, exist_ok=True)

# --- Google Drive 認証 ---
def login_drive():
    scope = ['https://www.googleapis.com/auth/drive']
    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(GCP_SA_KEY_PATH, scope)
    return GoogleDrive(gauth)

# --- JRADB設定 ---
JRADB_LIST_URL = "http://jradb.jp/jradb/listDownload.do"
# 必要なデータカテゴリとファイル拡張子の対応
TARGET_DATA = {
    "XHOS": "EXjrhos.dat.zip", # 馬マスタ(過去)
    "XRES": "EXjrsres.dat.zip", # 成績(過去)
    "JRES": "jrsres.dat.lzh",   # 成績(更新)
    "JHOS": "jrshos.dat.lzh",   # 馬マスタ(更新)
    "JJOC": "jrsjoc.dat.lzh",   # 騎手
    "JTRA": "jrstra.dat.lzh",   # 調教師
    "JTRE": "jrstre.dat.lzh",   # レコード
    "UMAS": "JracUmas.dat.zip", # 登録馬
}

# --- 最終更新日時の管理 ---
def get_last_update_time(drive):
    query = f"'{DRIVE_FOLDER_ID}' in parents and title = 'last_update.txt' and trashed=false"
    file_list = drive.ListFile({'q': query}).GetList()
    if file_list:
        return file_list[0].GetContentString().strip(), file_list[0]
    return "20200101000000", None

def update_last_update_time(drive, file_obj, new_time):
    if file_obj:
        file_obj.SetContentString(new_time)
        file_obj.Upload()
    else:
        new_file = drive.CreateFile({'title': 'last_update.txt', 'parents': [{'id': DRIVE_FOLDER_ID}]})
        new_file.SetContentString(new_time)
        new_file.Upload()

# --- メイン処理 ---
def process_update():
    try:
        drive = login_drive()
    except Exception as e:
        print(f"Google Drive認証エラー: {e}")
        return

    last_time, time_file_obj = get_last_update_time(drive)
    print(f"前回更新日時: {last_time}")

    # JRADBへリクエスト
    params = {"data": "store", "fromtime": last_time}
    try:
        res = requests.get(JRADB_LIST_URL, params=params, auth=(JRADB_USER, JRADB_PASS))
        res.raise_for_status()
    except Exception as e:
        print(f"JRADBアクセスエラー: {e}")
        return

    # XML解析
    try:
        # 名前空間の処理が面倒なので、文字列置換でタグを単純化してパース
        xml_content = res.content.decode('utf-8').replace(' xmlns=', ' ignore=')
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        nextexectime = root.findtext(".//nextexectime")
        urls = root.findall(".//url")
    except Exception as e:
        print(f"XML解析エラー: {e}")
        return

    download_list = []
    for u in urls:
        cat = u.get("category")
        link = u.text
        if cat in TARGET_DATA:
            download_list.append((cat, link))

    if not download_list:
        print("新しいデータはありません。")
        # 次回のために時刻だけ更新
        if nextexectime:
            update_last_update_time(drive, time_file_obj, nextexectime)
        return

    print(f"{len(download_list)} 件のファイルを処理します...")

    for cat, link in download_list:
        filename = os.path.basename(link)
        local_path = os.path.join(TEMP_DIR, filename)
        
        print(f"処理中: {filename}")
        
        # 1. ダウンロード
        try:
            r = requests.get(link, auth=(JRADB_USER, JRADB_PASS), stream=True)
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        except Exception as e:
            print(f"  ダウンロード失敗: {e}")
            continue

        # 2. 解凍 & アップロード
        extract_dir = os.path.join(TEMP_DIR, "extracted", cat)
        os.makedirs(extract_dir, exist_ok=True)
        
        success = False
        try:
            if filename.endswith(".zip"):
                with zipfile.ZipFile(local_path, 'r') as z:
                    z.extractall(extract_dir)
                success = True
            elif filename.endswith(".lzh"):
                lzh = lhafile.LhaFile(local_path)
                for info in lzh.infolist():
                    fname = info.filename
                    # LZHのファイル名はShift_JISの場合があるためデコード
                    # (今回はそのままバイナリ書き込みし、GoogleDrive側でファイル名処理)
                    with open(os.path.join(extract_dir, fname), "wb") as f:
                        f.write(lzh.read(info.filename))
                success = True
        except Exception as e:
            print(f"  解凍失敗: {e}")

        # 3. Google Driveへアップロード
        if success:
            # 親フォルダ: extracted
            q_cat = f"'{DRIVE_FOLDER_ID}' in parents and title = 'extracted' and trashed=false"
            extracted_parents = drive.ListFile({'q': q_cat}).GetList()
            if not extracted_parents:
                ep = drive.CreateFile({'title': 'extracted', 'mimeType': 'application/vnd.google-apps.folder', 'parents': [{'id': DRIVE_FOLDER_ID}]})
                ep.Upload()
                ep_id = ep['id']
            else:
                ep_id = extracted_parents[0]['id']

            # サブフォルダ: JRES, JHOS...
            q_sub = f"'{ep_id}' in parents and title = '{cat}' and trashed=false"
            sub_parents = drive.ListFile({'q': q_sub}).GetList()
            if not sub_parents:
                sp = drive.CreateFile({'title': cat, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [{'id': ep_id}]})
                sp.Upload()
                sp_id = sp['id']
            else:
                sp_id = sub_parents[0]['id']

            # ファイルアップロード
            for f_path in glob.glob(os.path.join(extract_dir, "*")):
                f_name = os.path.basename(f_path)
                print(f"  Driveへアップロード: {f_name}")
                # 同名ファイルがあるか確認（上書き回避または更新）
                q_file = f"'{sp_id}' in parents and title = '{f_name}' and trashed=false"
                existing_files = drive.ListFile({'q': q_file}).GetList()
                
                if existing_files:
                    drive_file = existing_files[0] # 更新
                else:
                    drive_file = drive.CreateFile({'title': f_name, 'parents': [{'id': sp_id}]}) # 新規
                
                drive_file.SetContentFile(f_path)
                drive_file.Upload()
        
        # クリーンアップ
        if os.path.exists(local_path): os.remove(local_path)
        if os.path.exists(extract_dir): shutil.rmtree(extract_dir)

    # 最後に時間を更新
    if nextexectime:
        update_last_update_time(drive, time_file_obj, nextexectime)
    print("全処理完了")

if __name__ == "__main__":
    process_update()
