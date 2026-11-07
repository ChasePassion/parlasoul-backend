# 本地 Milvus（开发用）

后端的记忆系统（`src/memory_system`）依赖 Milvus。本地开发用这套 compose 起一个单机 Milvus，
数据落在 `tools/milvus-local/volumes/`。

## 启动 / 停止

```bash
docker compose -f tools/milvus-local/compose.yaml up -d
docker compose -f tools/milvus-local/compose.yaml ps
docker compose -f tools/milvus-local/compose.yaml down
```

启动后：

- Milvus gRPC：`127.0.0.1:19530`
- 健康检查：`http://127.0.0.1:9091/healthz`

## 后端 `.env` 对应配置

```
MILVUS_URL=http://127.0.0.1:19530
MILVUS_TOKEN=            # 本地单机无鉴权，留空
MEMORY_COLLECTION_NAME=memories_dev
MEMORY_GROUP_COLLECTION_NAME=groups_dev
```

集合由后端首次启动记忆系统时自动创建（向量维度取 `MEMORY_EMBEDDING_DIM`，默认 2048）。

## 为什么必须本地有 Milvus

`pymilvus` 的 `MilvusClient` 默认 `timeout=None`，连不上时会在 gRPC channel-ready 上无限等待；
而 `MemoryRepository._get_memory()` 是在 asyncio 事件循环线程上同步调用的，会把整个后端冻住
（表现为发消息没有回复、`/health` 也超时）。Milvus 没起来时不要开本地后端。
