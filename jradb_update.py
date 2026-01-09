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
GCP_SA_KEY_PATH = "service_account.json"

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

# ★ここを修正：Colabの範囲に限定しつつ、将来の拡張性を確保
TARGET_DATA = {
    # === 【Colabで取得していたデータ (有効)】 ===
    # --- 蓄積系 (store) ---
    "XHOS": "EXjrshos.dat.zip", # 競走馬マスタ
    "JJOC": "jrsjoc.dat.lzh",   # 騎手マスタ
    "JTRA": "jrstra.dat.lzh",   # 調教師マスタ
    "XHSK": "EXjrhshsk.dat.zip",# JRA外成績
    "XOWN": "EXjrsown.dat.zip", # 馬主マスタ
    "XBRD": "EXjrsbrd.dat.zip", # 生産者マスタ
    "XSTB": "EXjrsstb.dat.zip", # 出馬表
    "XRES": "EXjrsres.dat.zip", # 競走馬成績
    "CRES": "chires.dat.lzh",   # 地方成績
    "XREC": "EXjrsrec.dat.zip", # レコード情報
    "JTRE": "jrstre.dat.lzh",   # 特別登録情報
    "UMAS": "JracUmas.dat.zip", # 新規馬名登録
    "DELU": "JracDelu.dat.zip", # 抹消競走馬

    # --- 速報系 (flash) ---
    "XSIN": "EXFSINFHEL.zip",   # 出走馬名表

    # === 【将来用 (現在は無効)】 ===
    # 必要になったらコメント(#)を外してください
    # "X200": "EXR200jkhr.zip",   # 速報成績(土日即時)
    # "I204": "I204jkhr.sss.lzh", # 翌日出走馬表
}

# --- 最終更新日時の管理 ---
def get_last_update_time(drive):
    query = f"'{DRIVE_FOLDER_ID}' in parents and title = 'last_update.txt' and trashed=false"
    file_list = drive.ListFile({'q': query}).GetList()
    if file_list:
        return file_list[0].GetContentString().strip(), file_list[0]
    # 初回デフォルト
    return "20240101000000", None

def update_last_update_time(drive, file_obj, new_time):
    if file_obj:
        file_obj.SetContentString(new_time)
        file_obj.Upload()
    else:
        new_file = drive.CreateFile({'title': 'last_update.txt', 'parents': [{'id': DRIVE_FOLDER_ID}]})
        new_file.SetContentString(new_time)
        new_file.Upload()

# --- XML解析とダウンロードリスト作成 ---
def process_data_type(data_type, last_time):
    """
    data_type: 'store' または 'flash'
    """
    print(f"\n--- [{data_type}] モードで確認中 ---")
    params = {"data": data_type, "fromtime": last_time}
    
    try:
        res = requests.get(JRADB_LIST_URL, params=params, auth=(JRADB_USER, JRADB_PASS))
        res.raise_for_status()
    except Exception as e:
        print(f"JRADBアクセスエラー({data_type}): {e}")
        return None, None

    try:
        # namespace問題を回避する簡易パース
        xml_content = res.content.decode('utf-8').replace(' xmlns=', ' ignore=')
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        nextexectime = root.findtext(".//nextexectime")
        urls = root.findall(".//url")
    except Exception as e:
        print(f"XML解析エラー: {e}")
        return None, None

    # ダウンロード対象の抽出
    download_list = []
    if urls is not None:
        for u in urls:
            cat = u.get("category")
            link = u.text
            # TARGET_DATA にキーが含まれているものだけ取得
            if cat in TARGET_DATA:
                download_list.append((cat, link))

    return download_list, nextexectime

# --- メイン処理 ---
def process_update():
    try:
        drive = login_drive()
    except Exception as e:
        print(f"Google Drive認証エラー: {e}")
        return

    last_time, time_file_obj = get_last_update_time(drive)
    print(f"前回更新日時: {last_time}")

    final_next_time = last_time
    has_update = False

    # store(蓄積) と flash(速報) の両方をチェック
    # ※XSINがflashに含まれるため両方見る必要があります
    for dtype in ["store", "flash"]:
        d_list, next_time = process_data_type(dtype, last_time)
        
        if next_time and next_time > final_next_time:
            final_next_time = next_time

        if not d_list:
            print(f"  > {dtype}: 対象ファイルなし")
            continue

        has_update = True
        print(f"  > {dtype}: {len(d_list)} 件のファイルを処理します...")

        for cat, link in d_list:
            filename = os.path.basename(link)
            local_path = os.path.join(TEMP_DIR, filename)
            
            print(f"    ダウンロード中: {filename}")
            
            # 1. ダウンロード
            try:
                r = requests.get(link, auth=(JRADB_USER, JRADB_PASS), stream=True)
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            except Exception as e:
                print(f"    ダウンロード失敗: {e}")
                continue

            # 2. 解凍
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
                        with open(os.path.join(extract_dir, fname), "wb") as f:
                            f.write(lzh.read(info.filename))
                    success = True
            except Exception as e:
                print(f"    解凍失敗: {e}")

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

                # カテゴリフォルダ
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
                    
                    q_file = f"'{sp_id}' in parents and title = '{f_name}' and trashed=false"
                    existing_files = drive.ListFile({'q': q_file}).GetList()
                    
                    if existing_files:
                        print(f"    Drive更新: {f_name}")
                        drive_file = existing_files[0]
                    else:
                        print(f"    Drive新規: {f_name}")
                        drive_file = drive.CreateFile({'title': f_name, 'parents': [{'id': sp_id}]})
                    
                    drive_file.SetContentFile(f_path)
                    drive_file.Upload()
            
            # 掃除
            if os.path.exists(local_path): os.remove(local_path)
            if os.path.exists(extract_dir): shutil.rmtree(extract_dir)

    # 最後に時間を更新
    if has_update or final_next_time > last_time:
        update_last_update_time(drive, time_file_obj, final_next_time)
        print(f"次回取得開始位置を更新しました: {final_next_time}")
    
    print("全処理完了")

if __name__ == "__main__":
    process_update()
