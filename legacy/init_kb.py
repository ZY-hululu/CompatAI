import os
import re
from dotenv import load_dotenv
load_dotenv()
import chromadb
import json
from openai import OpenAI

# ================== 配置区 ==================
load_dotenv()

API_KEY = os.getenv("DASHSCOPE_API_KEY")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_MODEL = "qwen3.7-text-embedding"

CHROMA_DB_PATH = "./chroma_db"
COLLECTION_NAME = "browser_compat"
DATA_DIR = "./data"

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)


def parse_md_file(filepath):
    """
    解析单个 .md 文件。
    格式：
      ## 标题
      **入库文本:**
      xxx...
      **元数据:**
      {"category": "JS", "property": "const", ...}
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 按 ## 切分成多条记录（跳过第一个空段和# JS兼容性数据这种大标题）
    records = re.split(r'\n##\s+', content)

    documents = []
    metadatas = []

    for record in records:
        record = record.strip()
        if not record:
            continue

        # 提取入库文本
        doc_match = re.search(
            r'\*\*入库文本[：:]\*\*\s*\n(.*?)(?=\*\*元数据[：:]\*\*|$)',
            record, re.DOTALL
        )
        # 提取元数据 JSON
        meta_match = re.search(
            r'\*\*元数据[：:]\*\*\s*\n\s*(\{.*?\})',
            record, re.DOTALL
        )

        if doc_match and meta_match:
            doc_text = doc_match.group(1).strip()
            meta_json = meta_match.group(1).strip()

            try:
                meta_dict = json.loads(meta_json)
                documents.append(doc_text)
                metadatas.append(meta_dict)
            except json.JSONDecodeError:
                print(f"  ⚠️ JSON解析失败，跳过: {filepath}")

    return documents, metadatas

# ================== 批量读取 data/ 目录 ==================
def load_all_data():
    """读取 data/ 目录下所有 .md 文件"""
    all_documents = []
    all_metadatas = []

    if not os.path.exists(DATA_DIR):
        print(f"⚠️ 数据目录 {DATA_DIR} 不存在")
        return all_documents, all_metadatas

    md_files = sorted([
        f for f in os.listdir(DATA_DIR) if f.endswith(".md")
    ])

    if not md_files:
        print(f"⚠️ data/ 下没有找到 .md 文件")
        return all_documents, all_metadatas

    for filename in md_files:
        filepath = os.path.join(DATA_DIR, filename)
        docs, metas = parse_md_file(filepath)
        print(f"  📄 {filename}: {len(docs)} 条")
        all_documents.extend(docs)
        all_metadatas.extend(metas)

    print(f"  总计: {len(all_documents)} 条\n")
    return all_documents, all_metadatas


# ================== 调用百炼 API 获取 Embedding ==================
def get_embeddings(texts):
    """分批调用百炼API生成向量，每批最多25条"""
    all_embeddings = []  # 空列表，用来存所有向量
    batch_size = 10  # 每批25条（百炼API限制）

    # 把文本列表切成多批，每批25条
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]  # 取第i到i+25条
        # 调用API，把这25条转成向量
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=batch
        )
        # 从响应里提取向量
        batch_embeddings = [item.embedding for item in resp.data]
        # 把这一批的向量加到总列表里
        all_embeddings.extend(batch_embeddings)

    return all_embeddings  # 返回所有向量


# ================== 主流程 ==================
def main():
    print("=" * 50)
    print("浏览器兼容性知识库 - 批量入库")
    print("=" * 50)

    # 1. 读取数据
    print("\n[1/3] 读取 data/ 目录...")
    documents, metadatas = load_all_data()

    if not documents:
        print("❌ 没有读取到数据，程序退出")
        return

    # 2. 生成向量
    print("[2/3] 调用百炼API生成向量...")
    embeddings = get_embeddings(documents)
    print(f"  生成 {len(embeddings)} 个向量，维度: {len(embeddings[0])}\n")

    # 3. 写入 ChromaDB（全量重建）
    print("[3/3] 写入 ChromaDB...")
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"description": "浏览器兼容性知识库"}
    )

    ids = [f"compat_{i}" for i in range(len(documents))]

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas
    )

    print(f"  ✅ 写入 {len(documents)} 条到集合: {COLLECTION_NAME}")
    print("\n" + "=" * 50)
    print("入库完成！")
    print("=" * 50)


if __name__ == "__main__":
    main()
