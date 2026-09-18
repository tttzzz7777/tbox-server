# tbox-server 开发工作流

> 多 agent 协作的工作流定义。Agent 定义在 `.claude/agents/`，本文档讲 main 怎么调度。

## 角色

| 角色 | 工具能力 | 关注点 |
|---|---|---|
| **PM** (`tbox-server-pm.md`) | Read, Bash, Glob, Grep, Write, Edit, SendMessage, ListAgents | 用户视角：要做什么、验收标准、边界场景、异常处理 |
| **Architect** (`tbox-server-architect.md`) | Read, Bash, Glob, Grep, SendMessage, ListAgents | 技术视角：文件改动、接口契约、数据模型、风险、任务分配 |
| **Backend** (`tbox-server-backend.md`) | Read, Write, Edit, Bash, Glob, Grep | 改 `tbox_server/*.py` / `handlers/` / `run_server.py` |
| **Frontend** (`tbox-server-frontend.md`) | Read, Write, Edit, Bash, Glob, Grep | 改 `tbox_server/webui/*` |
| **Tester** (`tbox-server-tester.md`) | Read, Bash, Glob, Grep（**无 Write/Edit**） | 跑 pytest + curl + playwright，只验证、不改码 |

## 主调度流程（main 视角）

```
用户需求
  │
  ▼
[1] spawn PM ──→ product spec v1
  │
  ▼
[2] spawn Architect(spec v1)
       │
       ├─ FINAL plan ──────────────────┐
       │                                │
       └─ ISSUES for PM                 │
            │                           │
            ▼                           │
       [3] spawn PM(反馈) ──→ spec v2    │
            │                           │
            └─────► spawn Architect(v2) ┘
                  │
                  └─ FINAL plan（含"调试顺序"）
  │
  ▼
[4] 串行派工（**禁止并行**，详见"调试串行规则"）
       │
       ├─ [4a] 先派的一方（按 plan 标注的顺序）
       │
       └─ [4b] 等回报后，再派另一方
  │
  ▼
[5] spawn Tester
  │
  ▼
[6] 汇报用户
```

**关键点**：
- main 是唯一调度者；PM / Architect / Engineer 之间不直接 spawn 对方
- Architect ↔ PM 最多 2 轮（v1 + v2），第 2 轮 Architect 必须收敛出 FINAL
- **Backend 和 Frontend 必须串行派工**（即使 plan 标了"无依赖"），原因见下
- Tester 总在最后

## 调试串行规则（**硬性约束**）

**禁止**同时 spawn Backend 和 Frontend。

### 为什么

- Backend 改 .py 需要 `./tboxctl restart`，会瞬断所有 in-flight 请求
- Frontend 用 playwright 跑 UI 验证，依赖 server 处于稳定可响应状态
- 两个 agent 并行调试会：
  - 抢同一份 `server.log` 输出，排查时不知道哪个 agent 的日志
  - Backend 重启正好打断 Frontend 的 playwright 测试，得到莫名其妙的失败
  - Backend 推一个 broken 的 commit，Frontend 测出来 fail 但其实根因在后端，Frontend 改前端无法修复
  - 同一秒可能有两个 `tboxctl restart` 撞车，导致 server 进入不一致状态

### 串行顺序（Architect 在 plan 里显式标注）

| 场景 | 先派 | 后派 |
|---|---|---|
| 后端新增 endpoint / 改协议 / 改 storage | **Backend**（先保证 server 接口稳定） | Frontend |
| 前端纯 UI / CSS / 文案改动，后端无改动 | **Frontend**（server 不用重启） | — |
| 两者都有改动（典型场景） | **Backend**（先把 endpoint 跑通 + restart 完） | Frontend（playwright 验） |
| 两者独立且无 server 改动 | 任意（Architect 在 plan 里写明） | — |

### 后端完成定义

Backend agent 在返回报告前必须确认：
- [ ] `./tboxctl restart` 已执行
- [ ] `./tboxctl status` 是 `running` + `health: ok`
- [ ] 自己改的 endpoint 用 curl 跑过
- [ ] server.log 最后 20 行无 ERROR
- [ ] server 处于"可以给前端用"的状态

### 前端完成定义

Frontend agent 在返回报告前必须确认：
- [ ] **不重启 server**（直接用 Backend 留下的 running 状态）
- [ ] playwright 跑过自己改的 UI
- [ ] 没 pageerror
- [ ] 如果测出新 endpoint，**先 curl 一下确认后端真的返回**（避免给后端的 bug 背锅）

### 异常处理

- Backend 调试失败 → main 让 Backend 重做，不切到 Frontend
- Frontend 调试发现疑似后端 bug → Frontend 用 curl 确认是不是后端问题；是的话**报告 main**，不修；main 重派 Backend
- Tester 在最后跑，会做最终裁决

## Agent 间通信

- **PM → Architect / Engineer**：通过 product spec（main 转交）
- **Architect → PM**：通过 "ISSUES" 列表（main 转交）
- **Architect → Engineer**：通过 tech plan（main 转交）
- **Engineer / Tester → PM**：通过完成报告（main 转交）
- 不直接 SendMessage（除了 PM/Architect 可选地用 SendMessage 互探，但这不是主路径）

## 边界规则

| 改什么 | 谁改 |
|---|---|
| `tbox_server/*.py` / `handlers/` / `run_server.py` / `simulator/` / `scripts/` / `tests/` | **Backend** |
| `tbox_server/webui/*` | **Frontend** |
| `data/` 下的运行数据 | **不要碰**（只读排查用） |
| `TBOXHELPER_PROTOCOL.md` / `PROTOCOL.md` | **不改**（除非 Architect 显式标注） |
| `requirements*.txt` / `pyproject.toml` | **慎重**：必须 Architect 显式标注 + 通报用户 |

## 触发条件

- 用户给需求 → 启动主调度
- 用户问项目问题 → 直接 main 回答，不走 workflow
- 用户改单文件 bug → main 直答或派 Backend，不走完整 workflow（PM / Architect 跳过）

## 修改这个 workflow

编辑对应 agent 的 `.md` 文件（frontmatter 或正文），无需改 main。
