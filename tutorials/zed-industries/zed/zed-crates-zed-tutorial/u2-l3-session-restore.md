# 会话恢复与首启引导

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `RestoreOnStartupBehavior` 四个变体的语义，以及 `restorable_workspace_locations` 中三个实际分支各自返回什么。
2. 沿着「SQLite `workspaces` 表 → `SessionWorkspace` → `SerializedMultiWorkspace` → 真实窗口」这条链路，讲清一次会话恢复的完整数据变换过程。
3. 解释 `last_session_window_stack` 存在时为什么必须对 locations 做 `reverse()`，才能还原上次退出时的窗口叠放顺序。
4. 列出恢复失败时的三层兜底：toast 提示、兜底空窗口、`FIRST_OPEN` 首启引导，以及它们各自的触发条件。

本讲聚焦 `restore_or_create_workspace` 与 `restorable_workspace_locations` 这对函数；它们「何时被调用」（启动竞态、CLI 打开循环）属于下一讲 u2-l4 的内容，这里只做铺垫。

## 2. 前置知识

本讲建立在 u2-l2 已建立的概念之上，先快速回顾并补充三块新概念。

**回顾：Session 与 AppSession 的拆分。** u2-l2 讲过，`Session` 是纯数据（本次 `session_id`、上次 `session_id`、上次窗口栈），`AppSession` 是包着它的 GPUI Entity，负责每 500ms 把窗口栈落盘并在退出时做最终保存。本讲会真正消费这三个字段中的后两个：`last_session_id()` 与 `last_session_window_stack()`。

**新概念 1：KVP 存储（KeyValueStore）。** Zed 用一个进程内键值数据库（`db::kvp::KeyValueStore`，GPUI 全局）存放「不属于任何工作区」的小状态：session id、窗口栈、首启标记 `first_open`、每个窗口的 `multi_workspace_state` 等。读 `read_kvp(key)` 返回 `Result<Option<String>>`——`Ok(None)` 表示「键不存在」，`Err(..)` 表示读取失败，本讲会看到代码对这两者的区分。

**新概念 2：窗口栈（window stack）。** 操作系统维护的窗口叠放顺序，最前面（最活跃）的窗口排第 0 位。GPUI 通过 `cx.window_stack()` 暴露它。它是「恢复后哪个窗口应该在最前面」这一问题的唯一信息来源。

**新概念 3：序列化工作区。** 一个「工作区」（workspace）= 一组项目根路径 + 打开状态（面板、分组、侧栏等）。退出时每个窗口的活动工作区被序列化进 SQLite 的 `workspaces` 表（带 `session_id`、`window_id` 列），窗口级状态另存 KVP。恢复就是把这两类数据读回来重放。

**涉及的设置项。** `settings.json` 中的 `restore_on_startup`（本讲主角）与 KVP 键 `first_open`（首启标记）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs) | 本讲主角：`restore_or_create_workspace`（L1418）与 `restorable_workspace_locations`（L1585），以及各调用点 |
| [crates/session/src/session.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs) | `Session`/`AppSession`：上次 session id 与窗口栈的产生、落盘、读取 |
| [crates/workspace/src/persistence.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs) | 数据层：SQL 查询、路径过滤、按窗口栈排序、按窗口分组、KVP 状态读取 |
| [crates/workspace/src/persistence/model.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence/model.rs) | 数据模型：`SessionWorkspace`、`MultiWorkspaceState`、`SerializedMultiWorkspace` |
| [crates/workspace/src/workspace.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs) | 执行层：`restore_multiworkspace`（L9638）等四个恢复辅助函数 |
| [crates/settings_content/src/workspace.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/workspace.rs) | `RestoreOnStartupBehavior` 枚举定义（L492） |
| [crates/onboarding/src/onboarding.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/onboarding/src/onboarding.rs) | `FIRST_OPEN` 常量与 `show_onboarding_view` |
| [crates/zed/src/zed.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs) | 测试范例：`test_multi_workspace_session_restore`（L7179） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**恢复行为枚举**（4.1）、**序列化工作区读取**（4.2）、**失败兜底与空窗口**（4.3）。

### 4.1 恢复行为枚举与决策入口

#### 4.1.1 概念说明

「Zed 启动后第一件事做什么」由用户设置 `restore_on_startup` 决定。它定义在 settings_content crate（workspace crate 的 `WorkspaceSettings` 由此生成），共有四个变体：

```rust
pub enum RestoreOnStartupBehavior {
    /// Always start with an empty editor tab
    #[serde(alias = "none")]
    EmptyTab,
    /// Restore the workspace that was closed last.
    LastWorkspace,
    /// Restore all workspaces that were open when quitting Zed.
    #[default]
    LastSession,
    /// Show the launchpad with recent projects (no tabs).
    Launchpad,
}
```

见 [crates/settings_content/src/workspace.rs:492-503](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/workspace.rs#L492-L503)。四个变体（serde snake_case，即设置里写 `"last_session"` 等）：

| 变体 | 设置值 | 语义 | `restorable_workspace_locations` 的返回 |
| --- | --- | --- | --- |
| `EmptyTab` | `empty_tab`（旧别名 `none`） | 空编辑器标签页 | `None`（不恢复） |
| `LastWorkspace` | `last_workspace` | 只恢复最后关闭的那个工作区 | `Some(vec![单个 SessionWorkspace])` |
| `LastSession`（默认） | `last_session` | 恢复上次退出时全部打开的工作区 | `Some(vec![..])` 或 `None` |
| `Launchpad` | `launchpad` | 显示启动面板 | `None`（不恢复） |

注意：枚举有 4 个变体，但决策函数的 `match` 实际只有 3 个分支——`LastWorkspace`、`LastSession`、其余（`EmptyTab`/`Launchpad`）统一落到 `_ => None`，由上层走「空窗口/启动面板」路径。

#### 4.1.2 核心流程

决策入口 `restorable_workspace_locations` 的逻辑：

```text
读取 restore_on_startup 设置 与 WorkspaceDb 全局
读取 Session 的两个字段:
    last_session_id            ← 上次运行的 session_id（KVP key "session_id"）
    last_session_window_stack  ← 上次退出时的窗口栈（KVP key "session_window_stack"）

若设置是 LastSession 但 last_session_id 不存在（首次运行/数据库为空）:
    降级为 LastWorkspace          # 合理：没有任何"上次会话"可恢复

match 设置:
    LastWorkspace => 查最近工作区，包成单元素列表（window_id 为 None）
    LastSession   => 按上次 session 查全部工作区（见 4.2），若有窗口栈则排序+reverse
    其他          => None
```

两个输入字段从哪来？看 session crate。`Session::new` 在启动早期创建新 session id 之前，先把旧值读出来存进内存：

```rust
pub async fn new(session_id: String, db: KeyValueStore) -> Self {
    let old_session_id = db.read_kvp(SESSION_ID_KEY).ok().flatten();

    db.write_kvp(SESSION_ID_KEY.to_string(), session_id.clone())
        .await
        .log_err();

    let old_window_ids = db
        .read_kvp(SESSION_WINDOW_STACK_KEY)
        ...
```

见 [crates/session/src/session.rs:15-38](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L15-L38)：先读旧的 `session_id` 与 `session_window_stack`（都存 KVP，键定义在 [session.rs:11-12](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L11-L12)），再把本次新 id 写回去。也就是说「上次」的信息在进程启动那一刻就被快照进 `Session` 结构体（[session.rs:5-9](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L5-L9)），后续恢复期间无论 KVP 怎么变都不影响。两个访问器非常薄：

- [`last_session_id()`：session.rs:118-120](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L118-L120) 返回 `Option<&str>`；
- [`last_session_window_stack()`：session.rs:127-129](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L127-L129) 返回 `Option<Vec<WindowId>>`。

窗口栈本身由 `AppSession` 维护（u2-l2 讲过其 500ms 轮询循环，[session.rs:73-96](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L73-L96)），退出钩子 `app_will_quit`（[session.rs:105-112](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L105-L112)）做最后一次保存。`window_stack()` 把平台的前到后窗口列表转成 id 数组（[session.rs:132-139](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L132-L139)），而平台接口就是 GPUI 的 [`App::window_stack`：gpui/src/app.rs:1224-1226](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/gpui/src/app.rs#L1224-L1226)。

#### 4.1.3 源码精读

现在读决策函数本体 [crates/zed/src/main.rs:1585-1654](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1585-L1654)。第一步，取设置与数据库句柄、取 session 的两个快照字段（[main.rs:1589-1604](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1589-L1604)：注意设置用的是 `WorkspaceSettings::get(None, cx)`，`None` 表示不按工作区局部化，取全局值）。

接着是降级逻辑（[main.rs:1606-1613](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1606-L1613)）：

```rust
if last_session_id.is_none()
    && matches!(
        restore_behavior,
        workspace::RestoreOnStartupBehavior::LastSession
    )
{
    restore_behavior = workspace::RestoreOnStartupBehavior::LastWorkspace;
}
```

「想恢复上次会话，但没有上次会话」→ 退而求其次恢复最近的工作区。这是第一处兜底。

`LastWorkspace` 分支（[main.rs:1616-1627](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1616-L1627)）调用 workspace crate 的薄封装 [`last_opened_workspace_location`：workspace/src/workspace.rs:9616-9625](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9616-L9625)，它最终落到 `WorkspaceDb::last_workspace`（[persistence.rs:2172-2174](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2172-L2174)）：取 `recent_project_workspaces` 的第一项。`recent_project_workspaces`（[persistence.rs:2066-2070](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2066-L2070) → `recent_project_workspaces_ungrouped` L2019）会过滤掉磁盘上已不存在的本地路径、失效的远程连接和 WSL 路径，所以「最近工作区已被删除」时这里自然返回 `None`。结果被包成单元素 `Vec<SessionWorkspace>`，`window_id` 置 `None`（这个工作区不属于任何待恢复窗口）。

`LastSession` 分支（[main.rs:1628-1651](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1628-L1651)）：

```rust
if let Some(last_session_id) = last_session_id {
    let ordered = last_session_window_stack.is_some();

    let mut locations = workspace::last_session_workspace_locations(
        &db,
        &last_session_id,
        last_session_window_stack,
        app_state.fs.as_ref(),
    )
    .await
    .filter(|locations| !locations.is_empty());

    // Since last_session_window_order returns the windows ordered front-to-back
    // we need to open the window that was frontmost last.
    if ordered && let Some(locations) = locations.as_mut() {
        locations.reverse();
    }

    locations
} else {
    None
}
```

三个细节值得咀嚼：

1. `ordered` 标志来自「窗口栈是否存在」——有栈才有「用户指定的顺序」可言，reverse 也只应在排序发生后执行。
2. `.filter(|locations| !locations.is_empty())` 把空列表折叠成 `None`，让上层统一走「无东西可恢复」路径（首启引导/空窗口）。
3. `reverse()` 的完整解释放在 4.2.3 末尾与综合实践，那里能看到排序发生在哪一侧。

数据侧的排序在 [`WorkspaceDb::last_session_workspace_locations`：persistence.rs:2180-2225](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2180-L2225)：先按 `session_id` 查出全部工作区（SQL 见 [persistence.rs:1880-1887](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L1880-L1887)，`WHERE session_id = ?1 ORDER BY timestamp DESC`），过滤掉磁盘上不存在的本地路径（[persistence.rs:2205-2212](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2205-L2212)），然后若给了窗口栈就按栈内位置排序：

```rust
if let Some(stack) = last_session_window_stack {
    workspaces.sort_by_key(|workspace| {
        workspace
            .window_id
            .and_then(|id| stack.iter().position(|&order_id| order_id == id))
            .unwrap_or(usize::MAX)
    });
}
```

见 [persistence.rs:2215-2222](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2215-L2222)。栈里越靠前（position 越小）排得越靠前；不在栈里的窗口（比如栈数据部分丢失）统一排到末尾（`usize::MAX`）。**所以这个函数返回的顺序是「最前面的窗口在前」。**

#### 4.1.4 代码实践

**实践目标**：验证四个设置值各自对应的启动行为，加深对「3 个 match 分支」的直观印象。

**操作步骤**：

1. 找到用户设置文件（Linux 下通常为 `~/.config/zed/settings.json`），把 `"restore_on_startup"` 分别设为 `"empty_tab"`、`"last_workspace"`、`"last_session"`、`"launchpad"`。
2. 每次修改后：打开 Zed → 打开一两个项目窗口 → 完全退出 → 重新启动。
3. 观察启动后窗口的数量与内容。

**需要观察的现象**：

- `last_session`：重启后应还原退出时的所有窗口（含各窗口活动的项目）。
- `last_workspace`：无论退出时开了几个窗口，只恢复一个「最近用过的工作区」。
- `empty_tab` / `launchpad`：不恢复任何项目，分别是空文件标签页与启动面板。
- 删除设置项恢复默认（`last_session`）。

**预期结果**：行为与 4.1.1 表格一致。若某次实验与预期不符，优先检查退出方式（「退出」与「关闭最后一个窗口」可能走不同序列化时机）与数据目录中 db 是否按通道隔离（u1-l1 讲过 dev/stable 通道共存）。本实践需要本地图形环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LastSession` 且 `last_session_id == None` 时选择降级为 `LastWorkspace`，而不是直接返回 `None`（空窗口）？

**答案**：`last_session_id` 为 `None` 只说明「数据库里没有上次会话的记录」（比如全新安装、或 db 被清空），但 `workspaces` 表里可能仍有带时间戳的历史工作区可恢复。降级到 `LastWorkspace` 能让老用户在升级/异常后仍然回到熟悉的目录；只有连最近工作区都没有时才落到空窗口路径。

**练习 2**：`recent_project_workspaces` 为什么要做磁盘存在性过滤？如果把过滤去掉，恢复流程会发生什么？

**答案**：工作区记录里的路径可能已被删除或移动（U 盘拔掉、目录改名）。恢复一个不存在的路径会让 `Workspace::new_local` 失败、增加 `error_count`，用户每次启动都看到「Failed to restore ...」toast。提前过滤把「注定失败的恢复」静默剔除，是「在数据层止损优于在 UI 层报错」的典型取舍。

**练习 3**：`restorable_workspace_locations` 里设置读取为什么用 `WorkspaceSettings::get(None, cx)` 而不是针对某个具体工作区读取？

**答案**：此刻窗口还不存在，谈不上「某个工作区」的局部设置；恢复行为是应用级决策，必须取全局值（`None` 即全局）。局部（项目级）设置要等对应 worktree 打开后才有意义。

### 4.2 序列化工作区的读取与重建

#### 4.2.1 概念说明

决策函数只回答「恢复哪些位置」；真正把位置变成窗口的是本模块。数据经历三级形态：

1. **DB 行**：`workspaces` 表中带 `session_id`、`window_id`、`paths`、`remote_connection_id` 的记录。
2. **`SessionWorkspace`**（[persistence/model.rs:55-63](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence/model.rs#L55-L63)）：`workspace_id + location（Local/Remote）+ paths + window_id`，「恢复一个工作区所需的最小信息」，纯内存结构。
3. **`SerializedMultiWorkspace`**（[persistence/model.rs:119-126](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence/model.rs#L119-L126)）：`active_workspace: SessionWorkspace + state: MultiWorkspaceState`，「恢复一个窗口所需的信息」。其中 `MultiWorkspaceState`（[model.rs:108-117](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence/model.rs#L108-L117)）是从 KVP 读回的窗口级状态：活动工作区 id、侧栏开关、项目分组键列表、侧栏面板状态。

为什么要分「位置」和「状态」两级？因为一个窗口（MultiWorkspace）可以装多个工作区，但同一时刻只有一个活动工作区；DB 记录所有工作区，KVP 记录每个窗口「当时正在看哪个」，两者 join 后才能精确还原用户视角。

#### 4.2.2 核心流程

```text
restorable_workspace_locations 产出 Vec<SessionWorkspace>   (前→后排序，已 reverse 为打开顺序)
        │
        ▼
workspace::read_serialized_multi_workspaces(locations)     [persistence.rs:327]
        │  按 window_id 把工作区分组（无 window_id 的各自成组）
        │  每组: 从 KVP 读 MultiWorkspaceState（键 multi_workspace_state/<window_id>）
        │  每组: 选出 active_workspace
        │       ① state.active_workspace_id 能匹配组内工作区 → 选它
        │       ② 否则选第一个 paths 非空的
        │       ③ 否则选第 0 个
        ▼
Vec<SerializedMultiWorkspace>  （一个元素 = 一个待开窗口）
        │
        ▼
restore_or_create_workspace 逐个处理:                      [main.rs:1425]
        ├─ location == Local  → workspace::restore_multiworkspace
        └─ location == Remote → RemoteSettings 补全 ssh 参数 → open_remote_project
                                → apply_restored_multiworkspace_state
        失败 → log + error_count += 1（进入 4.3 的兜底）
```

#### 4.2.3 源码精读

**分组与选活动工作区**：[`read_serialized_multi_workspaces`：persistence.rs:327-374](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L327-L374)。分组逻辑（[persistence.rs:334-347](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L334-L347)）：相同 `window_id` 的工作区进同一组，`window_id` 为 `None` 的（比如 `LastWorkspace` 分支造出的那个）各自单独成组。窗口状态从 KVP 读取（[`read_multi_workspace_state`：persistence.rs:304-312](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L304-L312)，作用域键 `multi_workspace_state`，读失败返回默认值——又一层静默降级）。活动工作区的三级回退（[persistence.rs:349-372](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L349-L372)）源码注释写得很清楚：持久化的活动工作区指针可能因行被清理而失配，此时不能盲目取 index 0，否则会把「游离的草稿/空工作区」恢复成焦点窗口——所以优先选第一个真正有路径的。

**主循环**：[`restore_or_create_workspace`：main.rs:1418-1575](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1418-L1575) 的前半段（[main.rs:1423-1475](https://github.com/zed-industries/zed/blob/a7d7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1423-L1475)）先经 `restorable_workspaces`（[main.rs:1577-1583](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1577-L1583)）把两步串起来，然后对每个 `SerializedMultiWorkspace` 按 `active_workspace.location` 分流：

- `SerializedWorkspaceLocation::Local`（枚举见 [model.rs:42-46](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence/model.rs#L42-L46)）→ `restore_multiworkspace`（[main.rs:1427-1431](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1427-L1431)）。
- `Remote(connection_options)` → 先用 `RemoteSettings::get_global(cx).fill_connection_options_from_settings(options)` 把用户 settings.json 里的远程参数（端口、用户名等）补进旧连接选项，再 `open_remote_project` 打开、`apply_restored_multiworkspace_state` 回放状态（[main.rs:1432-1468](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1432-L1468)）。这一步补全很关键：数据库里存的连接参数可能是旧机器上的，设置文件才是当前用户意图。

任何一项失败只记日志并 `error_count += 1`（[main.rs:1471-1474](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1471-L1474)），**不会中断其余窗口的恢复**——多窗口场景下一个坏工作区不该拖垮全部。

**单个窗口的恢复**：[`restore_multiworkspace`：workspace/src/workspace.rs:9638-9715](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9638-L9715)。打开方式二选一（[workspace.rs:9648-9667](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9648-L9667)）：`paths` 非空走 `Workspace::new_local`（按路径全新打开）；`paths` 为空（比如曾是无项目的草稿窗口）走 `open_workspace_by_id`（直接按 DB 主键恢复完整序列化状态）。主打开失败时还有第二层兜底（[workspace.rs:9669-9703](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9669-L9703)）：遍历 `state.project_groups` 里的每个项目分组，逐个尝试用其路径 `new_local`，成功一个即止——「活动工作区开不了，至少把侧栏里的某个项目开出来」。最后 `apply_restored_multiworkspace_state`（[workspace.rs:9717 起](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9717-L9728)）回放侧栏/分组状态，并调用 `window.activate_window()`（[workspace.rs:9709-9712](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L9709-L9712)）。

**现在可以完整回答 reverse 的问题**了。排序后的 locations 是「最前面的窗口在前」，而主循环**按列表顺序逐个打开**，且每个 `restore_multiworkspace` 结尾都 `activate_window()`——后激活的窗口最终位于窗口栈顶。若不 reverse，列表第一个（本应最前的）窗口会被后续窗口逐一盖到下面，恢复完成后「最前面」的变成列表最后一个，窗口叠放顺序恰好整体颠倒。`reverse()` 把「前→后」翻成「后→前」的打开顺序，使每个窗口被后续 activate 依次压栈后，最终栈序与上次退出时一致。而当 `last_session_window_stack` 为 `None`（旧数据/读取失败）时 `ordered == false`：没有排序就没有「用户顺序」可还原，保持 SQL 的 `ORDER BY timestamp DESC`（最近操作的在前）直接打开。

#### 4.2.4 代码实践

**实践目标**：通过阅读一个真实测试，验证「分组 → 选活动工作区 → 逐窗口恢复」的全链路。

**操作步骤**：

1. 打开 [crates/zed/src/zed.rs:7179](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L7179) 的 `test_multi_workspace_session_restore`。
2. 通读三条主线：
   - 构造阶段（L7199-7267）：窗口 A 装 dir1+dir2 两个工作区、窗口 B 装 dir3，并刻意把 A 的活动工作区切回 dir1（为恢复后「dir1 仍活动」的断言埋伏笔）；
   - 落盘阶段（L7270-7272）：调用 `flush_workspace_serialization`（定义在 [zed.rs:2961-2981](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2961-L2981)，主动刷新各 workspace 与 MultiWorkspace 的序列化任务，绕过节流）；
   - 模拟重启（L7295-7303）：关掉两个窗口后，用 `Session::test_with_old_session(session_id)` 替换 app_state 里的 session，让 `last_session_id()` 返回「刚才那个」session id——这正是生产环境里 `Session::new` 读 KVP 的测试替身。
3. 阅读断言（L7305-7393）：先直接查 `last_session_workspace_locations` 验证 3 条记录、2 个窗口分组；再调用 `crate::restore_or_create_workspace`（L7336-7339）走真实恢复路径，断言恢复出 2 个窗口、各窗口的活动工作区与项目分组正确。
4. 尝试运行：`cargo test -p zed test_multi_workspace_session_restore`。

**需要观察的现象**：测试如何在不启动真实进程的情况下复现「退出→重启」；`flush_workspace_serialization` 为什么要手动 await。

**预期结果**：测试通过；若你把 L7298-7303 的 `replace_session_for_test` 注释掉（不要提交），`last_session_id` 变为 `None`，恢复路径应降级为 `LastWorkspace`，恢复窗口数从 2 变 1，断言失败——这正好反向验证 4.1 的降级逻辑。完整构建 Zed 依赖较重，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`read_serialized_multi_workspaces` 选择活动工作区时，为什么「第一个 paths 非空的」优于「第 0 个」？

**答案**：一个窗口曾装过的工作区里可能有空/草稿工作区（paths 为空）。若持久化的 `active_workspace_id` 失配后盲目回退到 index 0，恢复出来的焦点窗口可能是个空白工作区，用户看到的是「我的项目全没了」。优先选有路径的工作区能保证焦点落在真实项目上；只有全组都没有路径（整组都是草稿）才取第 0 个。

**练习 2**：远程工作区恢复时为什么要 `fill_connection_options_from_settings`，而不是直接用数据库里存的连接参数？

**答案**：DB 里的 `RemoteConnectionOptions` 是上次连接时的快照，主机端口/用户名可能在当前机器的 settings.json 中被更新过（尤其换电脑迁移配置后）。以设置文件为准补全，能避免用过期参数连接失败；这也是「设置文件是用户当前意图，数据库是历史事实」的原则体现。

**练习 3**：主循环中一个工作区恢复失败，为什么选择「记日志继续」而不是立刻返回错误？

**答案**：`LastSession` 会恢复多个独立窗口，它们之间没有依赖。一个失效（比如目录被删）就中止全部，会让用户连其他完好窗口也拿不到。聚合计数 `error_count` 再统一上报（见 4.3），把「单点失败」的影响限制在单窗口。

### 4.3 失败兜底、首启引导与空窗口

#### 4.3.1 概念说明

恢复的终点不止「成功」一种。`restore_or_create_workspace` 的后半段处理四种非理想结局，层层递进：

1. **部分失败**：恢复了 N 个，挂了 M 个 → 在某个存活窗口上弹 toast。
2. **全部失败**：一个窗口都没开出来 → 开一个空窗口，在它上面弹同样的 toast。
3. **窗口数为 0 的静默结局**：`error_count == 0` 但一个窗口都没有（典型：用户在远程连接提示上点了取消，`open_remote_project` 返回 `Ok` 但窗口被移除）→ 不开 toast，直接开空窗口，否则 Zed 会「看似正常运行却无任何窗口」。
4. **无可恢复**：`restorable_workspaces` 返回 `None`（设置为 `EmptyTab`/`Launchpad`，或 `LastWorkspace`/`LastSession` 都查不到东西）→ 看 `FIRST_OPEN` 标记：首次启动显示 onboarding，否则开空窗口。

另外还有第 0 层：`restore_or_create_workspace` 自身返回 `Err`（连兜底空窗口都开不出来），由调用方的 `fail_to_open_window_async` 处理。

#### 4.3.2 核心流程

```text
error_count > 0 ?
 ├─ 是 → 有活跃 MultiWorkspace 窗口?
 │        ├─ 是 → 在其当前 workspace 上 show_toast("Failed to restore N workspace(s)...")
 │        └─ 否 → workspace::open_new 开空窗口 + 同样的 toast        [main.rs:1509-1525]
 └─ 否 → 跳过
cx.windows().is_empty() ?
 └─ 是 → workspace::open_new 开空窗口;
          restore_on_startup == Launchpad ? 什么都不加 : Editor::new_file   [main.rs:1532-1551]

（更早分支：restorable_workspaces 返回 None 时）
kvp.read_kvp(FIRST_OPEN) == Ok(None) ?        # 键不存在 = 从未完成首启
 ├─ 是 → show_onboarding_view(app_state)     # 打开欢迎页并写 first_open=false
 └─ 否 → open_new 空窗口（Launchpad 除外同上不加新文件）  [main.rs:1552-1572]
```

#### 4.3.3 源码精读

**toast 与兜底空窗口**：[main.rs:1477-1526](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1477-L1526)。错误消息按数量单复数措辞（"Failed to restore 1 workspace..." / "Failed to restore N workspaces..."），然后尝试找当前活跃窗口（`cx.active_window()` downcast 成 `MultiWorkspace`）在其 workspace 上 `show_toast`（[main.rs:1487-1505](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1487-L1505)）。找不到任何可用的活跃窗口时，开一个空窗口并把 toast 放上去（[main.rs:1507-1525](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1507-L1525)）——保证「错误一定有人看见」。

**窗口数为 0 的静默退出防护**：[main.rs:1528-1551](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1528-L1551)。源码注释直接点明动机：用户在启动时取消了失败的远程连接，`open_remote_project` 返回 `Ok` 但移除了窗口，`error_count` 保持 0，上面的 toast 兜底不会触发；没有这个检查 Zed 会静默「无窗口运行」。空窗口的内容构建回调里，只有 `Launchpad` 模式不加 `Editor::new_file`（启动面板本身不要多余的新文件标签页）：

```rust
let restore_on_startup = WorkspaceSettings::get_global(cx).restore_on_startup;
match restore_on_startup {
    workspace::RestoreOnStartupBehavior::Launchpad => {}
    _ => {
        Editor::new_file(workspace, &Default::default(), window, cx);
    }
}
```

**首启引导分支**：[main.rs:1552-1572](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1552-L1572)。判定条件是 `matches!(kvp.read_kvp(FIRST_OPEN), Ok(None))`——注意这同时排除了 `Err`：KVP 读取失败时**不会**误判为首启（宁可跳过 onboarding 也不打扰老用户）。`FIRST_OPEN` 是 onboarding crate 的常量 `"first_open"`（[onboarding.rs:56](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/onboarding/src/onboarding.rs#L56)，在 main.rs L39 导入）。[`show_onboarding_view`：onboarding.rs:183-206](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/onboarding/src/onboarding.rs#L183-L206) 打开一个新工作区窗口、把 `Onboarding` 页面加入中心区并聚焦，同时发 `Onboarding Page Opened` 遥测事件，最后**在打开动作里**就把 `first_open` 写为 `"false"`（[onboarding.rs:199-203](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/onboarding/src/onboarding.rs#L199-L203)）——写标记不等用户关闭欢迎页，防止「看过一次但中途崩溃 → 每次启动都弹」。

**第 0 层兜底**：`restore_or_create_workspace` 本身返回 `Err` 时（例如兜底 `open_new` 也失败），调用方走 [`fail_to_open_window_async`：main.rs:152-154](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L152-L154) → [`fail_to_open_window`：main.rs:156](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L156)：stderr 打印错误与排查链接；非 Linux 直接 `process::exit(1)`，Linux/FreeBSD 则尝试发桌面通知后再退出。

**四个调用点**（谁会触发恢复）：

| 调用点 | 位置 | 场景 |
| --- | --- | --- |
| 启动主路径 | [main.rs:940-948](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L940-L948) | `app.run` 闭包里，启动时 open 队列为空（或仅 focus-app）则恢复会话；恢复任务与 `first_window_rx` 的竞态属于 u2-l4 |
| CLI 无参数打开 | [open_listener.rs:848-853](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L848-L853) | 已在运行的 Zed 收到 `zed`（不带路径）时，等价于「给我开点东西」→ 走恢复 |
| FocusApp 请求 | [main.rs:1009-1017](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1009-L1017) | `zed://focus` 之类请求：先尝试激活既有窗口，一个都没有才恢复 |
| macOS dock 重开 | [main.rs:465-476](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L465-L476) | `app.on_reopen`：用户点 dock 图标而窗口全关时恢复会话 |

恢复完成后还有一次善后：启动路径会把 `restore_task` 与垃圾回收串联（[main.rs:960-974](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L960-L974)），等恢复结束后调用 `garbage_collect_workspaces`（[persistence.rs:2121-2170](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs#L2121-L2170)）清理无法再恢复的工作区行——注意它刻意保留当前 session 与上个 session 的工作区（注释写明「让进行中的恢复还能补水分」），所以必须排在恢复之后。

#### 4.3.4 代码实践

**实践目标**：亲眼看一次「恢复失败 → toast」兜底。

**操作步骤**：

1. 备份并记录 `restore_on_startup: "last_session"`，打开两个不同目录的项目窗口，完全退出 Zed（让 session 与窗口栈落盘）。
2. 把其中一个项目目录重命名（或移到别处）。
3. 重新启动 Zed。

**需要观察的现象**：存活窗口上是否出现 "Failed to restore 1 workspace. Check logs for details." toast；另一个窗口是否正常恢复；日志文件中是否有 `Failed to restore workspace:` 前缀的错误（路径见 u1-l4 讲过的 logs 目录）。

**预期结果**：`recent_project_workspaces`/`last_session_workspace_locations` 通常会在数据层就过滤掉磁盘上不存在的本地路径（见 4.1/4.2），所以更可能的现象是「坏窗口被静默跳过、不弹 toast」——这本身就是值得记录的观察：数据层过滤生效时，UI 层兜底根本轮不到触发。想强制触发 toast，可以改用「目录存在但无读权限」等仍能通过存在性检查、却在打开时失败的形态。需要本地图形环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「窗口数为 0」的检查（main.rs:1532）放在 toast 兜底之后、且独立于 `error_count`？

**答案**：存在 `error_count == 0` 但窗口为 0 的路径——远程连接被用户取消时 `open_remote_project` 返回 `Ok` 却移除了窗口。toast 兜底只看 `error_count`，覆盖不了这种情况，所以需要独立检查兜底。它同时自然覆盖了「`error_count > 0` 且所有窗口都失败」的子集场景（此时空窗口已由 toast 分支打开，`windows()` 非空，检查通过不重复开窗）。

**练习 2**：`matches!(kvp.read_kvp(FIRST_OPEN), Ok(None))` 与 `kvp.read_kvp(FIRST_OPEN).ok().flatten() == None` 有什么行为差异？这里选前者是有意的吗？

**答案**：前者在 `Err(..)` 时不匹配（不会进 onboarding 分支）；后者把 `Err` 折叠成 `None`（会进 onboarding 分支）。KVP 读失败大概率是环境问题而非「新安装」，此时弹欢迎页只会打扰老用户，所以「读失败 ≠ 首启」的语义更安全，前者的写法是有意的。

**练习 3**：`show_onboarding_view` 为什么在打开欢迎页的同一步就写 `first_open = "false"`，而不是等用户看完关闭页面再写？

**答案**：若等关闭再写，用户看了一眼欢迎页就杀进程/崩溃，标记仍是缺失状态，下次启动又会弹欢迎页，陷入「每次启动都弹」。打开即写把 onboarding 语义定为「至少展示过一次」，牺牲了「中途退出还想再看」的小场景，换取不会重复打扰——与 restore 链路里「尽早落盘、宁少勿多」的整体风格一致。

## 5. 综合实践

把本讲三个模块串成一张完整的恢复流程图（对应本讲实践任务第一问）。以下即参考答案，建议你先合上讲义自己画一遍，再对照：

```text
触发: 启动(main.rs:940) / CLI 无参数(open_listener.rs:852) / FocusApp(main.rs:1014) / dock 重开(main.rs:469)
        │
        ▼
restore_or_create_workspace(app_state, cx)                     main.rs:1418
        │
        ▼
restorable_workspaces → restorable_workspace_locations         main.rs:1577 / 1585
        │  读 restore_on_startup + WorkspaceDb
        │  读 Session 快照: last_session_id, last_session_window_stack
        │  LastSession 无 last_session_id → 降级 LastWorkspace
        │
        ├── LastWorkspace → db.last_workspace() → vec![SessionWorkspace { window_id: None }]
        ├── LastSession   → db.last_session_workspace_locations(session_id, stack)
        │                      ├ SQL: WHERE session_id=?1 ORDER BY timestamp DESC
        │                      ├ 过滤: 磁盘不存在的本地路径 / 失效远程连接
        │                      ├ 有 stack → 按 stack 位置排序（最前的窗口在最前）
        │                      └ 空列表折叠为 None；非空且 ordered → reverse()
        └── EmptyTab / Launchpad → None
        │
        ▼
   返回 None?
   ├─ 否 → read_serialized_multi_workspaces                    persistence.rs:327
   │        │ 按 window_id 分组；无 window_id 各自成组
   │        │ 每组读 KVP multi_workspace_state/<window_id> → MultiWorkspaceState
   │        │ 每组选 active: active_workspace_id 匹配 → 首个有 paths → 第 0 个
   │        ▼
   │    for SerializedMultiWorkspace（打开顺序 = reverse 后顺序）:   main.rs:1425
   │        ├ Local → restore_multiworkspace                     workspace.rs:9638
   │        │   ├ paths 非空 → Workspace::new_local
   │        │   ├ paths 空 → open_workspace_by_id
   │        │   ├ 失败 → 遍历 project_groups 兜底 new_local
   │        │   └ apply_restored_multiworkspace_state → activate_window
   │        └ Remote → RemoteSettings 补全参数 → open_remote_project → apply state
   │    失败 → log + error_count+=1，不中断
   │        ├ error_count>0 → 活跃窗口 show_toast；无窗口则开空窗口+toast   main.rs:1477-1526
   │        └ windows 为空  → 开空窗口（Launchpad 不加新文件）             main.rs:1532-1551
   └─ 是 → KVP 无 first_open 键?
            ├ 是 → show_onboarding_view（写 first_open=false）  onboarding.rs:183
            └ 否 → open_new 空窗口（Launchpad 不加新文件）      main.rs:1554-1572
        │
        ▼
（启动路径）restore_finished 后 → garbage_collect_workspaces   main.rs:960-974
```

第二问（`last_session_window_stack` 存在时为何 reverse）的完整论证链，见 4.2.3 末尾。浓缩成一句话：**排序产出「前→后」的展示序，而逐窗口打开 + `activate_window()` 的语义是「后开的在顶」，两种顺序语义相反，reverse 负责换序。**

延伸小任务（选做）：把上图与 `Session::new`（session.rs:15-38）、`AppSession` 的 500ms 落盘循环（session.rs:73-96）对照，在图上用虚线标出 `last_session_id` 与 `last_session_window_stack` 两个数据的生产点——你会发现它们都产生于「上一次进程」，本次进程只是消费者，这正是 u2-l2 讲过的「Session 快照」设计的用意。

## 6. 本讲小结

- `RestoreOnStartupBehavior` 有 4 个变体，但 `restorable_workspace_locations` 的 match 只有 3 个实际分支：`LastWorkspace` 返回单元素列表、`LastSession` 返回上次会话全部工作区、其余返回 `None` 交给空窗口/启动面板路径。
- 恢复数据经历三级形态：DB `workspaces` 行 → `SessionWorkspace`（最小位置信息）→ `SerializedMultiWorkspace`（按 `window_id` 分组 + KVP 窗口状态 + 三级回退选出活动工作区）。
- 窗口叠放顺序的还原依赖「按窗口栈排序（前→后）+ `reverse()`（换成打开顺序）+ 每窗口 `activate_window()`」三者配合；没有窗口栈时不排序也不 reverse。
- 失败处理分层：单个工作区失败记日志继续；有存活窗口则 toast，否则开空窗口 + toast；`error_count == 0` 但窗口为 0 也要开空窗口防止静默无窗运行。
- 首启引导由 KVP 键 `first_open` 缺失触发（读取失败不触发），`show_onboarding_view` 在打开当步即写回 `false` 防止重复打扰。
- 数据层尽量提前止损（过滤已消失的路径），UI 层兜底只处理数据层无法预判的失败——这是贯穿整条链路的设计取向。

## 7. 下一步学习建议

本讲结束了 u2「应用初始化主链路」的会话恢复部分。下一讲 **u2-l4 启动期打开请求循环** 会紧接着本讲的 `restore_task`：`OpenListener` 双端通道、首窗 `first_window_rx` 与 `restore_finished` 的竞态等待、`open_rx` 循环如何把请求交给 `handle_open_request`——你会看到本讲刻意略过的 main.rs:923-998 那段的完整含义。

若想继续深挖本讲相关源码，推荐：

1. [crates/workspace/src/persistence.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/persistence.rs) 的 `garbage_collect_workspaces`（L2121）与 `recent_project_workspaces_ungrouped`（L2019）——理解工作区行的生命周期与「七天才删」的宽限期设计。
2. [crates/zed/src/zed.rs:7397](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L7397) 的 `test_quit_preserves_focused_workspace_for_restore`——从「退出时保存焦点」的另一半视角看恢复。
3. 单元 3 的 u3-l1（`OpenRequest` 与 `zed://` URL 解析），本讲提到的 FocusApp 分支在那一讲会展开成完整的 URL 协议族。
