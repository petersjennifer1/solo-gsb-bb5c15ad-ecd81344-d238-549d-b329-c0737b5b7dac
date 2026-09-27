# 内网 CIDR 批量分配 API

基于 **Python + FastAPI + SQLite + 标准库 `ipaddress`** 的内网地址池批量分配服务。

地址池由一个**父网段**和若干**保留子网**组成。预演接口一次接收 **1～8 个前缀长度请求**，在剩余空间内搜索**全部可行的对齐子网组合**，按以下字典序选取唯一最优方案：

1. **先最小化**：分配完成后剩余空闲空间的规范 CIDR 数量（即尽量减少地址碎片）；
2. **再最小化**：按请求顺序排列的网络地址序列，取字典序最小者（即尽量让靠前的请求拿到更小的地址）。

提交接口只在**池版本未变化且方案仍可用**时，在单条数据库事务内**原子落库**；否则返回 `409 Conflict`，**不会发生部分分配**。

回收接口在单条数据库事务内**部分回收已分配地址**：接收池版本与一个规范 CIDR，从当前已分配地址中扣除该区间，并把仍被占用的部分整体重写为**最少的规范 CIDR**。

---

## 一条命令启动

```bash
docker compose up --build -d
```

该命令会一次性完成：

- 构建镜像并安装全部 Python 依赖；
- 初始化 SQLite 数据库（建表、写入父网段与保留子网）；
- 启动 API 服务。

> 数据库与依赖初始化均随 `docker compose up --build` 自动完成，无需任何手动迁移步骤。数据持久化在命名卷 `cidr-data`（容器内 `/data`）。

服务地址与端口：

- **API 基址**：`http://localhost:8000`
- **交互式文档（Swagger UI）**：<http://localhost:8000/docs>
- **OpenAPI Schema**：<http://localhost:8000/openapi.json>
- **健康检查**：`GET http://localhost:8000/healthz`

停止 / 清理：

```bash
docker compose down            # 停止并删除容器（保留数据卷）
docker compose down -v         # 同时删除数据卷（彻底重置）
```

---

## 必要配置

配置通过 `docker-compose.yml` 中的环境变量提供（也可在 `environment:` 中直接修改）：

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `PARENT_CIDR` | 是 | `10.0.0.0/8` | 地址池父网段，IPv4/IPv6 均可，如 `10.0.0.0/16`、`2001:db8::/48`。 |
| `RESERVED_CIDRS` | 否 | 空 | 保留子网列表，逗号分隔，必须全部包含在父网段内；如 `10.0.0.0/20,10.0.255.0/24`。 |
| `DB_PATH` | 否 | `/data/cidr_pool.db` | SQLite 数据库文件路径；建议保持在挂载卷 `/data` 下。 |

> 地址池配置仅在**数据库首次初始化**（数据卷为空）时生效。修改父网段/保留子网后若希望重新初始化，请先执行 `docker compose down -v` 再 `up`。

支持 IPv4 与 IPv6（二者不能混用在同一地址池）。

---

## API 一览

基址：`http://localhost:8000`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/api/pool` | 查询池状态：父网段、版本、保留/已分配子网、当前空闲 CIDR |
| POST | `/api/allocations/preview` | **预演**：不修改状态，返回池版本与最优方案 |
| POST | `/api/allocations/commit` | **提交**：携带版本与方案，原子落库或返回 409 |
| POST | `/api/allocations/reclaim` | **部分回收**：从已分配地址扣除一个规范 CIDR，原子重写或返回 409 |

### 1. 查询池状态

```bash
curl http://localhost:8000/api/pool
```

```json
{
  "parent_cidr": "10.0.0.0/8",
  "version": 1,
  "reserved_cidrs": ["10.0.0.0/16", "10.255.255.0/24"],
  "allocated_cidrs": [],
  "free_cidrs": ["10.1.0.0/16", "10.2.0.0/15", "..."],
  "free_cidr_count": 4
}
```

### 2. 预演分配

请求体 `prefix_lengths` 为 1～8 个前缀长度，**顺序即请求顺序**。

```bash
curl -X POST http://localhost:8000/api/allocations/preview \
  -H 'Content-Type: application/json' \
  -d '{"prefix_lengths": [24, 24, 23]}'
```

成功（方案可行）：

```json
{
  "pool_version": 1,
  "feasible": true,
  "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"],
  "free_cidr_count_after": 3,
  "free_cidrs_after": ["..."],
  "reason": null
}
```

- `pool_version`：预演时刻的池版本，提交时必须原样带回；
- `allocations`：与 `prefix_lengths` **同序**对齐的子网 CIDR，为全局最优方案；
- `free_cidr_count_after` / `free_cidrs_after`：按此方案分配后剩余的空闲 CIDR 数量与列表。

无可行方案时 HTTP 200 返回：

```json
{ "pool_version": 1, "feasible": false, "allocations": [],
  "free_cidr_count_after": 0, "free_cidrs_after": [],
  "reason": "no aligned subnet fits in free space" }
```

参数非法（空列表、超过 8 个、前缀越界等）返回 `422 Unprocessable Entity`。

### 3. 提交分配（原子）

把预演返回的版本号与方案原样提交：

```bash
curl -X POST http://localhost:8000/api/allocations/commit \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 1,
       "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"]}'
```

成功（版本自增、方案整体落库）：

```json
{
  "status": "committed",
  "pool_version": 2,
  "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"],
  "free_cidrs": ["..."]
}
```

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**数据库不发生任何写入**：

- 池版本已被其它提交改变（乐观并发控制）；
- 方案中的子网已被占用或与现存子网重叠；
- 方案越出父网段或内部重叠。

典型工作流：**先 preview 拿到 `pool_version` + `allocations`，再原样 commit**；若收到 409，则重新 preview 后重试。

### 4. 部分回收已分配地址（原子）

把**池版本**与一个**规范 CIDR** 提交到 `/api/allocations/reclaim`。该 CIDR 必须**完全由当前已分配地址覆盖**，可以恰好落在一条分配记录内部，也可以**跨越相邻的多条分配记录**：

```bash
curl -X POST http://localhost:8000/api/allocations/reclaim \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 3, "cidr": "10.10.250.0/23"}'
```

成功时在**单条 SQLite 写事务**内扣除目标地址，把仍被占用的地址整体重写为**最少的规范 CIDR**（相邻碎片会被合并），版本自增：

```json
{
  "status": "reclaimed",
  "pool_version": 4,
  "reclaimed_cidrs": ["10.10.250.0/23"],
  "allocated_cidrs": ["10.10.248.0/23"],
  "free_cidrs": ["10.1.0.0/16", "10.10.4.0/22", "..."]
}
```

- `reclaimed_cidrs`：本次实际回收生效的 CIDR（即请求的规范 CIDR）；
- `allocated_cidrs`：扣除目标地址后，**剩余仍被占用**的最少规范 CIDR 列表；
- `free_cidrs`：回收后当前全部空闲 CIDR。回收**立即生效**——随后的池状态查询与分配预演都会把释放出的空间视为空闲。

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**地址池不发生任何改动（版本也不自增）**：

- 池版本已过期（乐观并发控制）；
- 目标 CIDR 含**未分配**地址（包括跨越已分配块之间的空隙）；
- 目标 CIDR 含**保留**地址；
- 目标 CIDR **越出父网段**。

此外，CIDR 非规范（主机位非零，如 `10.10.1.5/24`）或字段缺失返回 `422 Unprocessable Entity`。若收到 409，重新查询 `/api/pool` 获取最新版本与占用情况后再决定是否重试。

---

## 正确性与并发保证

- 所有候选均为网络地址对齐的规范 CIDR（基于标准库 `ipaddress` 严格校验）；
- 搜索算法在二进制前缀树上做子集 DP（每请求集合状态至多 2^8=256 个），
  通过“节点几何 + 请求前缀多重集”的结构化记忆化与等前缀对称剪枝，
  穷举全部可行划分后比较目标函数，因此结果与完全暴力枚举一致
  （仓库测试含数千组 IPv4/IPv6 随机对照用例）；
- 预演与池状态查询在**同一条 SQLite 读事务（WAL 快照）**内读取池版本
  与子网：两次查询之间即使有并发提交落库，返回的版本与占用集也必然
  属于同一快照，不会拼出“旧版本 + 新占用集”的错位状态；
- 提交使用 SQLite `BEGIN IMMEDIATE` 单事务：版本检查 → 方案可用性复检
  （与保留/已分配子网逐一比对）→ 批量插入 → 版本自增。任一环节失败立即
  回滚，保证“全成功或全不写入”，并发提交同一版本时**恰有一个**成功；
- 提交响应中的版本、已分配子网与空闲网段在**本次写事务内部**读取，
  严格绑定本次提交；不会在提交后另开连接而混入其它并发提交的结果。
  （`tests/test_concurrency.py` 用可控的并发交错对以上两条读取不变量
  做了回归覆盖。）
- 部分回收同样使用 SQLite `BEGIN IMMEDIATE` 单事务：版本检查 → 覆盖性
  复检（目标必须完全落在已分配地址内、不含保留地址、不越出父网段）→
  删除并按“合并占用区间减去目标”重写为最少规范 CIDR → 版本自增。
  任一环节失败立即回滚，地址池与版本保持原样；同版本并发回收时
  **恰有一个**成功，回收后的空闲空间对池状态查询与后续分配预演立即可见。

---

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
DB_PATH=./cidr_pool.db PARENT_CIDR=10.0.0.0/16 \
  uvicorn app.main:app --reload --port 8000
```

运行测试（无需网络与第三方依赖即可跑算法对照测试）：

```bash
python3 -m tests.test_allocator_bruteforce   # 4000 组 IPv4 暴力对照
python3 -m tests.test_api                    # 接口端到端 + 冲突/原子性
python3 -m tests.test_concurrency            # 并发交错回归：快照读取与提交响应绑定
python3 -m tests.test_reclaim                # 部分回收：跨记录、最少 CIDR 重写、冲突与随机区间不变量
```

## 项目结构

```
app/
  allocator.py   # 区间运算 + 前缀树子集 DP 搜索算法（仅标准库）
  config.py      # 环境变量配置
  db.py          # SQLite 建表、状态读取、BEGIN IMMEDIATE 原子提交
  main.py        # FastAPI 路由与请求/响应模型
tests/
  test_allocator_bruteforce.py
  test_dense_reserved.py
  test_api.py
  test_concurrency.py
  test_reclaim.py
Dockerfile
docker-compose.yml
requirements.txt
```
