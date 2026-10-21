# 04_后端与部署（负责：C）

API、任务队列与状态机（QUEUED→PLANNING→RUNNING→VALIDATING→READY）、Worker、存储与签名下载、Docker Compose 部署配置。

## 代码与配置
| 文件 | 内容 |
|---|---|
| `zhihui/api/app.py` | FastAPI：建任务 / 轮询 / 反问回答 / 取消 / HMAC 签名下载；asyncio 队列 + Worker；托管前端静态页 |
| `zhihui/api/db.py` | SQLite：tasks、events 两张表；重启时把中断的任务标为失败 |
| `Dockerfile` | 单镜像（含中文字体 wqy-microhei）；API 和 MCP Server 用不同启动命令 |
| `docker-compose.yml` | api（对外 8000）+ mcp（仅内网 8765，令牌校验），共享数据卷 |
| `.env.example` | 环境变量模板 |

## 现状（10.13）
- [x] 本机单进程运行通过（`python -m zhihui.api --mock`），接口测试见 `tests/test_api.py`
- [x] MCP HTTP 模式 + 令牌已实测：不带令牌返回 401
- [ ] **Dockerfile / compose 尚未实测**（开发机上没装 Docker），10.21 前在云主机上构建并验证中文字体渲染
- [ ] 方案书写的是 Redis 队列；目前用进程内 asyncio 队列（单机足够）。如果要上 Redis，替换 `queue` 和 `worker()` 即可，接口不用改
- [ ] 超时与重试、日志落盘、压测（51 条基准并发跑）
- [ ] 演示备机：现场断网时用 `--mock` 加本机已生成的文件
