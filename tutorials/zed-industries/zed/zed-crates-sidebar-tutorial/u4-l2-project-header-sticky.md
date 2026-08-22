# 项目分组头与粘性头部渲染

## 1. 本讲目标

学完本讲，你应该能够：

1. 拆解 `render_project_header` 产出的「一行分组头」由哪些零件组成：标签、远程图标、折叠箭头、三个状态徽标、新建按钮、省略号菜单，以及它们各自的显示条件。
2. 解释 `is_group_collapsed` / `set_group_expanded` 这对读写方法为什么把折叠状态存放在 `MultiWorkspace` 而不是 `Sidebar` 自己身上，以及这条路径如何被序列化持久化。
3. 说明 `project_header_indices` 这个「分组头下标表」在重建时如何生成、又被哪些消费方使用。
4. 读懂 `render_sticky_header` 的吸附判定与「推开」式位移算法，理解它如何借助 `project_header_indices` 与 `ListState` 的滚动信息工作。
5. 对照 `test_collapse_and_expand_group` 与 `test_collapse_changes_entry_shape` 两个测试，说明折叠操作如何同时改变可见行集合与 `EntryShape` 序列。

## 2. 前置知识

本讲建立在 u4-l1（渲染主骨架）与 u2-l2（工作区与项目分组）之上，还需要回忆：

- **`ListEntry` 三种行**（u2-l1）：侧边栏列表的每一行是 `ListEntry` 枚举的一个变体——`ProjectHeader`（项目分组头）、`Thread`（线程行）、`Terminal`（终端行）。本讲的主角是 `ProjectHeader`。
- **`ProjectGroupKey`**（u2-l2）：项目分组的身份键＝主 worktree 路径列表＋可选的远程主机。同一窗口里 linked worktree 与主仓库共用一个键。折叠状态正是以它为键存放的。
- **`MultiWorkspace` 与弱引用宿主**（u1-l3）：`Sidebar` 通过 `WeakEntity<MultiWorkspace>` 反向持有宿主，访问时必须 `upgrade()` 判空。宿主是「窗口内多项目世界模型」的拥有者。
- **全量重推导教义**（u3-l2）：任何变化都汇入 `update_entries` → `rebuild_contents`，从当前世界状态**整表重算** `contents`。侧边栏自己尽量不存可推导的状态。
- **`EntryShape` 与测量保留**（u3-l3）：行的「等高身份键」。相等形状必须渲染出相同高度；形状变了，`apply_list_state_diff` 才会让该区间测量失效。回忆那个关键约束：`bounds_for_item` 对未测量的行返回 `None`——本讲的粘性头部正好依赖已测量的高度。
- **gpui 虚拟列表 `list` 与 `ListState`**（u4-l1）：只为视口内的行构建元素；`ListState` 缓存滚动位置与每行实测高度。粘性头部要回答的问题正是「视口顶端现在压着哪一行」。
- **FluentBuilder 条件链**：`.when(条件, |el| ...)`、`.when_some(Option, |el, 值| ...)`、`.map(|el| ...)`，读渲染代码的三板斧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 本讲主战场：`render_project_header`、`render_sticky_header`、`is_group_collapsed` / `set_group_expanded`、`toggle_collapse`、键盘折叠族（`expand_selected_entry` 等）、`cycle_project_impl`，以及 `rebuild_contents` 中生成 `project_header_indices` 的段落 |
| `crates/workspace/src/multi_workspace.rs` | 折叠状态的真正存放处：`ProjectGroupState`、`project_groups` 字段、`group_state_by_key(_mut)`、`set_all_groups_expanded`、`restore_project_groups` 与 `serialize` |
| `crates/sidebar/src/sidebar_tests.rs` | `test_collapse_and_expand_group`、`test_collapse_changes_entry_shape`、`test_collapse_state_survives_worktree_key_change`，以及断言辅助 `visible_entries_as_strings`、`assert_project_header_has_threads` |

## 4. 核心概念与源码讲解

### 4.1 `render_project_header`：一行分组头由哪些零件组成

#### 4.1.1 概念说明

项目分组头是侧边栏里「一个项目」的门牌：它汇总该分组下所有线程/终端的状态，让用户不展开也能一眼看到「这个项目有没有在跑、有没有在等我确认、有没有新通知」，同时提供折叠/展开、新建线程、分组菜单三个入口。

它的全部输入都在 `ListEntry::ProjectHeader` 变体里（重建时算好），加上两个运行时参数 `is_sticky`（是否作为粘性头部渲染）与 `is_focused`（键盘选中态）。也就是说：**渲染函数是纯投影，状态计算全部发生在 `rebuild_contents`**——这正是 u3-l2 教义在渲染层的体现。

先看数据侧。`ProjectHeader` 变体的字段（[sidebar.rs:L392-L405](https://github.com/zed-industries-zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L392-L405)）：

| 字段 | 含义 | 由谁计算 |
| --- | --- | --- |
| `key: ProjectGroupKey` | 分组身份键 | 分组遍历时来自 `MultiWorkspace.project_groups` |
| `label: SharedString` | 显示名（可含路径消歧后缀） | `group_key.display_name(&path_detail_map)` |
| `highlight_positions` | 搜索命中字符下标 | 重建时 fuzzy 匹配得到 |
| `has_running_threads` | 组内是否有 Running 线程 | 遍历 `live_infos`（活跃信息，进程内存态） |
| `waiting_thread_count` | 等待确认的线程数 | 同上，按 `WaitingForConfirmation` 计数 |
| `has_notifications` | 组内有未读通知 | 对照 `notified_threads` / `notified_terminals` 集合 |
| `is_active` | 是否为当前活跃工作区所在分组 | 与 `active_workspace` 比对 |
| `has_threads` | 组内是否有任何线程行 | 影响空态子行与 `EntryShape` |

#### 4.1.2 核心流程

渲染一行分组头的组装顺序：

```text
render_project_header(ix, is_sticky, key, label, ...徽标参数...)
│
├─ 1. 计算 id / group_name：is_sticky 决定是否加 "sticky-" 前缀
├─ 2. 查询折叠态 is_collapsed = is_group_collapsed(key)（现查 MultiWorkspace）
├─ 3. 左侧区（h_flex）
│     ├─ 标签 Label / HighlightedLabel（非活跃时 Muted 色；透明窗口下改截断不渐变）
│     ├─ 远程项目图标（可选，render_remote_project_icon）
│     ├─ [.when(is_collapsed)] 三个状态徽标：LoadCircle / Warning / Circle
│     └─ [.when(!has_filter)] 折叠箭头 ChevronRight / ChevronDown（hover 可见）
├─ 4. 右侧区（h_flex）：渐变遮罩 + 新建线程按钮 + 省略号菜单
│     └─ on_mouse_down(左键) → stop_propagation（防止冒泡到整行的点击折叠）
├─ 5. 整行事件
│     ├─ on_mouse_down(右键) → 打开分组上下文菜单
│     └─ on_click → 副键修饰 → 激活该组工作区；否则（且无搜索词时）→ toggle_collapse
└─ 6. 尾部分流：!is_collapsed && !has_threads → 附加 "No threads yet" 空态子行
```

其中第 3 步的徽标**只在折叠时显示**——展开时每条线程行有自己的状态图标，分组头没必要重复；折叠后子行不可见，状态就只能上收到门牌上。三者的优先级在代码里用 `.when` 条件串起来：运行中显示转圈 `LoadCircle`；等待确认显示 `Warning`（带 tooltip「N threads are waiting for confirmation」）；仅当既没有运行也没有等待时，通知才用 `Circle`（Accent 色）显示。

#### 4.1.3 源码精读

函数签名与 sticky 前缀。注意 `is_sticky` 只影响元素 id，不影响任何业务逻辑——粘性头部与列表内的行渲染**共用同一套代码**，只是元素身份不同（[sidebar.rs:L2259-L2287](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2259-L2287)）：

```rust
fn render_project_header(
    &self,
    ix: usize,
    is_sticky: bool,
    key: &ProjectGroupKey,
    ...
) -> AnyElement {
    let id_prefix = if is_sticky { "sticky-" } else { "" };
    let id = SharedString::from(format!("{id_prefix}project-header-{ix}"));
    ...
    let is_collapsed = self.is_group_collapsed(key, cx);
    let disclosure_icon = if is_collapsed {
        IconName::ChevronRight
    } else {
        IconName::ChevronDown
    };
```

折叠时上收的三个状态徽标（[sidebar.rs:L2362-L2400](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2362-L2400)）：外层 `.when(is_collapsed, ...)` 包住三个互斥分支，通知徽标额外要求 `!has_running_threads && waiting_thread_count == 0`，避免与转圈/警告叠在一起：

```rust
.when(is_collapsed, |this| {
    this.when(has_running_threads, |this| {
        this.child(Icon::new(IconName::LoadCircle)...with_rotate_animation(2))
    })
    .when(waiting_thread_count > 0, |this| {
        ... Icon::new(IconName::Warning) ...tooltip(...)
    })
    .when(has_notifications && !has_running_threads && waiting_thread_count == 0, |this| {
        this.child(Icon::new(IconName::Circle).color(Color::Accent))
    })
})
```

箭头与点击折叠都受 `has_filter` 约束（[sidebar.rs:L2401-L2411](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2401-L2411) 与 [sidebar.rs:L2444-L2452](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2444-L2452)）：搜索期间不显示折叠箭头，点击也不触发折叠，因为过滤视图的行集合是查询结果，不是分组语义的完整列表：

```rust
.when(!has_filter, |this| {
    this.child(div()
        .when(!is_focused, |this| this.visible_on_hover(&group_name))
        .child(Icon::new(disclosure_icon)...))
})
...
.on_click(cx.listener(move |this, event: &gpui::ClickEvent, window, cx| {
    if event.modifiers().secondary() {
        this.activate_or_open_workspace_for_group(&key_for_focus, window, cx);
    } else if !this.has_filter_query(cx) {
        this.toggle_collapse(&key_for_toggle, window, cx);
    }
}))
```

右侧按钮簇的防误触（[sidebar.rs:L2414-L2432](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2414-L2432)）：按钮簇包在自己的 `h_flex` 里，`on_mouse_down(左键)` 调 `cx.stop_propagation()`，否则点「+」会同时触发整行的折叠。

最后是空态子行（[sidebar.rs:L2455-L2477](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2455-L2477)）：**展开且没有线程**时，分组头下面追加一行半透明的「No threads yet」。注意这个子行是渲染层的产物，不占用 `entries` 的一个位置——但它让这一「逻辑行」的高度变了，这正是 u3-l3 中 `EntryShape::ProjectHeader` 必须携带 `has_threads` 与 `is_collapsed` 两个字段的原因（[sidebar.rs:L484-L499](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L484-L499)）：

```rust
if !is_collapsed && !has_threads {
    v_flex().w_full()
        .child(header)
        .child(h_flex()...
            .child(Label::new("No threads yet").size(LabelSize::Small)...))
        .into_any_element()
} else {
    header.into_any_element()
}
```

徽标的数据来源在 `rebuild_contents` 里：即便分组已折叠，`live_infos`（从组内所有打开工作区的 Agent 面板收集的活跃线程信息）仍被完整遍历一遍，统计 Running 与 WaitingForConfirmation（[sidebar.rs:L1704-L1716](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1704-L1716)，折叠分支的对称实现在 [sidebar.rs:L1759-L1765](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1759-L1765)）：

```rust
for info in live_infos {
    if info.status == AgentThreadStatus::Running {
        has_running_threads = true;
    }
    if info.status == AgentThreadStatus::WaitingForConfirmation {
        waiting_thread_count += 1;
    }
    live_info_by_session.insert(info.session_id.clone(), info);
}
```

#### 4.1.4 代码实践

1. **实践目标**：把分组头的「状态 → 图标 → 显示条件」整理成一张自查表，并验证你对显示条件的理解。
2. **操作步骤**：
   - 精读 [sidebar.rs:L2362-L2400](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2362-L2400)，填出下表（示例已给一行）：

     | 状态组合 | 图标 | 显示条件 |
     | --- | --- | --- |
     | 组内有 Running 线程且已折叠 | `LoadCircle`（旋转动画） | `is_collapsed && has_running_threads` |
     | 等待确认（待填） | ？ | ？ |
     | 仅有通知（待填） | ？ | ？ |

   - 再回答：展开状态下这三个徽标去哪了？
3. **需要观察的现象**：表格填完后，你会发现三者的显示条件经由 `.when` 嵌套天然互斥——通知徽标永远不会和转圈同时出现。
4. **预期结果**：等待确认 → `Warning` + tooltip；仅有通知 → `Circle`（Accent 色）；展开时状态显示在每条线程行自身上（u4-l3 详讲）。
5. 若想在真实 UI 中观察，可在本机 `cargo run -p zed` 打开侧边栏折叠一个有后台线程的分组；图标外观依赖主题，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么粘性头部渲染时传 `is_sticky = true`，而这个参数的实际作用只是改 id 前缀？

**答案**：分组头的视觉与行为由 `ListEntry::ProjectHeader` 的字段决定，粘性头部渲染的是「同一个下标 `ix` 的同一份数据」，理应像素级一致；但 gpui 中元素 id 参与焦点、hover 分组（`group_name`）、`visible_on_hover` 等机制，若与列表内的行共用 id，同一帧会出现两个同 id 元素导致状态错乱，所以用 `"sticky-"` 前缀区分元素身份。

**练习 2**：`has_threads` 字段明明可以从「entries 里该 header 之后到下一个 header 之前有没有 Thread/Terminal 行」推导出来，为什么还要作为字段存进 `ProjectHeader`？

**答案**：因为折叠时子行根本不会被 push 进 `entries`（见 4.3 节），折叠的分组头后面紧跟的就是下一个分组头；此时「组内是否有线程」无法从可见行推导，只能查元数据存储。而空态子行与 `EntryShape`（key、`has_threads`、`is_collapsed` 三元组）都依赖它，所以重建时显式算好存进变体。

### 4.2 `is_group_collapsed` / `set_group_expanded`：折叠状态为什么住在 MultiWorkspace

#### 4.2.1 概念说明

这对读写方法是侧边栏与宿主之间围绕折叠状态的唯一通道。读侧现查、写侧直改，而**状态本体存在 `MultiWorkspace.project_groups` 里**——`Vec<ProjectGroupState>`，每个元素是 `{ key, expanded }`。

为什么放宿主而不是 `Sidebar` 自己？综合源码有四条理由：

1. **分组的生命周期长于侧边栏的可见性**。分组（及其键）由 `MultiWorkspace` 在工作区加入时通过 `ensure_project_group_state` 创建、默认展开（[multi_workspace.rs:L669-L686](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L669-L686)），侧边栏关着分组也存在；折叠偏好自然应与分组同生共死。
2. **持久化路径已经在这里**。`MultiWorkspace::serialize` 把每个分组的 `expanded` 写进 `MultiWorkspaceState`，连同 `sidebar_open`、侧边栏自身序列化一起落库（[multi_workspace.rs:L1449-L1477](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1449-L1477)）；恢复时 `restore_project_groups` 合并历史状态（[multi_workspace.rs:L825-L846](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L825-L846)）。若折叠态存在 `Sidebar`，就得两条序列化链路。
3. **分组键会变，状态要跟着迁移**。给项目添加 worktree 时 `ProjectGroupKey` 从 `[/a]` 变成 `[/a, /b]`，`rekey_project_group` 负责把旧键的状态搬到新键（[multi_workspace.rs:L697-L708](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L697-L708)）——这是世界模型级别的维护，侧边栏无从知晓。
4. **全量重推导教义**。u3-l2 要求：凡是能从「当前世界状态」算出的都不该存进 `Sidebar`。折叠态虽然不是可推导的派生数据（它是用户偏好），但把它放进世界模型（宿主）后，`Sidebar` 保持纯投影——`is_group_collapsed` 每次渲染都现查，连 `EntryShape` 投影也不存副本（u3-l3 已见过：`entry_shapes` 实时调 `group_state_by_key`）。

#### 4.2.2 核心流程

一次完整折叠的时序：

```text
用户点击分组头（无搜索词）
  └─ toggle_collapse(key)                       [sidebar.rs:L3229]
       ├─ is_collapsed = is_group_collapsed(key)   ← 读：现查宿主
       ├─ set_group_expanded(key, is_collapsed)    ← 写：expanded 取反（collapsed → expanded=true）
       │    └─ mw.update:
       │         ├─ group_state_by_key_mut(key).expanded = expanded
       │         └─ mw.serialize(cx)               ← 异步写 KeyValueStore（持久化）
       └─ self.update_entries(cx)                  ← 手动同步重建
            └─ rebuild_contents:
                 ├─ is_collapsed = is_group_collapsed(key)  ← 重新现查
                 ├─ 折叠且无搜索词 → 不加载线程 → 只 push header
                 └─ entry_shapes 投影 is_collapsed → apply_list_state_diff
```

两个关键细节：

- `set_group_expanded` **不发出** `ProjectGroupsChanged` 事件（那是分组结构变化的事件，由移动/删除分组等操作发出，如 [multi_workspace.rs:L898-L913](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L898-L913) 的 `move_project_group_up`）。所以 `toggle_collapse` 必须自己补一句 `update_entries`，不能指望订阅网络刷新——注意这与 u3-l1 的「事件驱动刷新」并不矛盾：折叠是侧边栏自己发起的写，发起者自然负责收尾。
- 侧边栏对宿主事件的订阅里，`ProjectGroupsChanged` 与 `WorkspaceRemoved` 共用一个分支，统一走 `schedule_update_entries` 去抖（[sidebar.rs:L813-L832](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L813-L832)）。

#### 4.2.3 源码精读

读方法：升级弱引用 → 读宿主 → 查表取反；任何一步失败都回退为「展开」（[sidebar.rs:L930-L950](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L930-L950)）：

```rust
fn is_group_collapsed(&self, key: &ProjectGroupKey, cx: &App) -> bool {
    self.multi_workspace
        .upgrade()
        .and_then(|mw| {
            mw.read(cx)
                .group_state_by_key(key)
                .map(|state| !state.expanded)
        })
        .unwrap_or(false)
}

fn set_group_expanded(&self, key: &ProjectGroupKey, expanded: bool, cx: &mut Context<Self>) {
    if let Some(mw) = self.multi_workspace.upgrade() {
        mw.update(cx, |mw, cx| {
            if let Some(state) = mw.group_state_by_key_mut(key) {
                state.expanded = expanded;
            }
            mw.serialize(cx);
        });
    }
}
```

状态本体与访问器在宿主侧（[multi_workspace.rs:L279-L288](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L279-L288)、[multi_workspace.rs:L879-L896](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L879-L896)）——线性查找、按需创建、批量置位都集中在这里：

```rust
pub struct ProjectGroupState {
    pub key: ProjectGroupKey,
    pub expanded: bool,
}
...
pub fn group_state_by_key(&self, key: &ProjectGroupKey) -> Option<&ProjectGroupState> {
    self.project_groups.iter().find(|group| group.key == *key)
}

pub fn set_all_groups_expanded(&mut self, expanded: bool) {
    for group in &mut self.project_groups {
        group.expanded = expanded;
    }
}
```

重建时对折叠的消费不止「跳过子行」：折叠且无搜索词时干脆不查询线程存储（`should_load_threads`，[sidebar.rs:L1543-L1544](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1543-L1544)），但会另行查询线程 id 以维护通知集合（[sidebar.rs:L1908-L1923](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1908-L1923) 的注释解释了这一点）；push header 后用 `continue` 跳过子行入列（[sidebar.rs:L1930-L1944](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1930-L1944)）：

```rust
let is_collapsed = self.is_group_collapsed(group_key, cx);
let should_load_threads = !is_collapsed || !query.is_empty();
...
project_header_indices.push(entries.len());
entries.push(ListEntry::ProjectHeader { ... });
if is_collapsed {
    continue;
}
```

键盘折叠族（u5-l2 会从键位角度再讲，这里只看它们如何复用这对读写方法）：`SelectChild` 在分组头上是「展开或下移」（[sidebar.rs:L4250-L4272](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L4250-L4272)）；`SelectParent` 在线程行上是「收起到父级」——先回溯找到所属分组头、把 selection 移过去再折叠（[sidebar.rs:L4274-L4304](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L4274-L4304)）；`FoldAll`/`UnfoldAll` 直接走宿主的批量接口（[sidebar.rs:L4341-L4367](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L4341-L4367)）：

```rust
fn collapse_selected_entry(...) {
    ...
    Some(ListEntry::Thread(_) | ListEntry::Terminal(_)) => {
        for i in (0..ix).rev() {
            if let Some(ListEntry::ProjectHeader { key, .. }) = self.contents.entries.get(i) {
                let key = key.clone();
                self.selection = Some(i);
                self.set_group_expanded(&key, false, cx);
                self.update_entries(cx);
                break;
            }
        }
    }
```

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：用源码证据回答两个问题——「折叠状态为何持久化在 MultiWorkspace 上」与「折叠如何影响 EntryShape」。
2. **操作步骤**：
   - 顺着调用链通读：[sidebar.rs:L930-L950](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L930-L950)（读写方法）→ [multi_workspace.rs:L879-L890](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L879-L890)（状态表）→ [multi_workspace.rs:L1449-L1477](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1449-L1477)（序列化把 `expanded` 落库）→ [multi_workspace.rs:L697-L708](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L697-L708)（rekey 迁移状态）。为每处写一句「它证明了什么」。
   - 运行两个测试并阅读断言：
     ```bash
     cargo test -p sidebar --lib test_collapse_and_expand_group
     cargo test -p sidebar --lib test_collapse_changes_entry_shape
     ```
   - 对照 [sidebar_tests.rs:L950-L1000](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L950-L1000)：`visible_entries_as_strings` 对 `ProjectHeader` 行输出的图标 `v`/`>` 正是 `is_group_collapsed` 的回读（[sidebar_tests.rs:L563-L570](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L563-L570)）；折叠后只剩 `"> [my-project]"` 一行，说明子行没有进 `entries`。
   - 对照 [sidebar_tests.rs:L721-L751](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L721-L751)：它在 `toggle_collapse` 前后各快照一次 `entry_shapes`，断言两个序列**不相等**。
3. **需要观察的现象**：两个测试均通过；第一个测试输出展示 `v → >` 的行集合收缩，第二个测试无输出但断言成立。
4. **预期结果**：折叠改变 `EntryShape` 序列有两条途径——`ProjectHeader` 形状里的 `is_collapsed` 字段翻转（见 [sidebar.rs:L2053-L2071](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2053-L2071) 的投影），同时组内的 `Thread(id)` 形状整段消失。于是 `apply_list_state_diff` 判定「这一行高度可能变了」，让该区间测量失效——这正确，因为空态子行「No threads yet」的出现/消失确实改变高度。这也回答了「为何持久化在 MultiWorkspace」：`Sidebar` 是纯投影，凡非派生状态都上交宿主，由宿主统一序列化与迁移。
5. 附加观察（可选）：[sidebar_tests.rs:L1003-L1051](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L1003-L1051) 的 `test_collapse_state_survives_worktree_key_change` 验证了分组键从 `[/project-a]` 变为 `[/project-a, /project-b]` 后折叠态仍保留——rekey 迁移的直接证据。

#### 4.2.5 小练习与答案

**练习 1**：`set_group_expanded` 写完后没有 `cx.emit`，也没有 `cx.notify()`（对 Sidebar 自身），刷新靠什么？

**答案**：靠调用方手动补 `update_entries`。`toggle_collapse`、`expand_selected_entry`、`collapse_selected_entry`、`fold_all` 等每个写点之后都紧跟一次 `self.update_entries(cx)`；`update_entries` 收尾的 `cx.notify()` 才触发重渲染。折叠不发出 `ProjectGroupsChanged`，因为该事件语义是「分组集合/结构变了」，而折叠只是既有分组上的一个布尔翻转。

**练习 2**：如果 `ensure_project_group_state` 尚未为某个键创建状态（例如分组刚出现、状态表还没更新），`is_group_collapsed` 返回什么？这合理吗？

**答案**：`group_state_by_key` 返回 `None`，`unwrap_or(false)` 使其表现为「展开」。合理——新分组默认展开（`ensure_project_group_state` 里 `expanded: true`），`None` 与「默认值」语义一致，避免了调用方判空。

**练习 3**：`Sidebar::new` 的订阅里已经有 `ProjectGroupsChanged → schedule_update_entries`，为什么 `toggle_collapse` 不改成「`set_group_expanded` 内部发一个事件」来复用这条链路？

**答案**：可以但没必要，且语义会变混。折叠需要**同步**完成重建（`toggle_collapse` 里 `update_entries` 是同步调用，测试也依赖 `run_until_parked` 前状态就绪）；事件链路是去抖合并的 `schedule_update_entries`，语义是「世界变了、稍后统一重算」。用结构变化事件驱动布尔翻转会让一个事件承担两种粒度，反而模糊了「一级订阅管世界结构」的分层。

### 4.3 `project_header_indices`：从列表下标到分组头的反向索引

#### 4.3.1 概念说明

`SidebarContents` 里除了 `entries` 本体，还维护一个 `project_header_indices: Vec<usize>`（[sidebar.rs:L475-L482](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L475-L482)）——**按出现顺序记录每个 `ProjectHeader` 在 `entries` 里的下标**。

它解决的问题是：`entries` 是「拍平」的一维数组，分组结构（哪个范围属于哪个组）在拍平过程中丢失了；而粘性头部、项目循环切换都需要回答「下标 `i` 往前最近的分组头是谁」「全部分组头按顺序是哪些」。每次全量重建时，这个索引与 `entries` 同步重新生成，因此永远一致——不需要维护，这是「派生索引进快照」的模式。

#### 4.3.2 核心流程

生成：`rebuild_contents` 的分组遍历循环里，push header **之前**先记录当前 `entries.len()`（这正是 header 即将落位的下标）：

```text
for group_key in 分组序列:
    （收集线程/终端、计算徽标……）
    project_header_indices.push(entries.len())   ← header 将要到的下标
    entries.push(ListEntry::ProjectHeader { ... })
    if is_collapsed: continue                     ← 折叠组：后面没有子行
    push 子行（线程/终端）……
```

展开与折叠两个分支各有一次这样的 push（[sidebar.rs:L1884](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1884) 与 [sidebar.rs:L1930](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1930)），最终作为 `SidebarContents` 的字段一起落进快照（[sidebar.rs:L1965-L1969](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1965-L1969)）。

举例，一个两分组（第一组展开含 2 行、第二组折叠）的列表：

```text
entries:                  [HeaderA, T1, T2, HeaderB]
                          ↓生成
project_header_indices:   [0, 3]
```

消费方有三处：

| 消费方 | 问题 | 用法 |
| --- | --- | --- |
| `render_sticky_header` | 视口顶部压着哪个组的头？下一个头在哪？ | 反向 `find`（≤ 滚动行号的最后一个）；正向 `find`（> 当前头的第一个） |
| `active_project_header_position` | 活跃工作区所在分组的头是第几个？ | `position` 遍历比对 key |
| `cycle_project_impl` | `NextProject`/`PreviousProject` 切到哪？ | 以 `header_count` 取模循环 |

#### 4.3.3 源码精读

「先记下标再 push」的一行代码（展开分支，[sidebar.rs:L1884-L1894](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1884-L1894)）：

```rust
project_header_indices.push(entries.len());
entries.push(ListEntry::ProjectHeader {
    key: group_key.clone(),
    label,
    ...
});
```

项目循环切换的用法：`cycle_project_impl` 把「活跃分组头在索引表中的位置」作为游标，对 `header_count` 取模前进/后退，再经索引表映射回 entries 下标取 key（[sidebar.rs:L6998-L7042](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6998-L7042)）：

```rust
let header_count = self.contents.project_header_indices.len();
...
let next_pos = match current_pos {
    Some(pos) => {
        if forward { (pos + 1) % header_count }
        else { (pos + header_count - 1) % header_count }
    }
    None => 0,
};
let header_entry_ix = self.contents.project_header_indices[next_pos];
```

而 `active_project_header_position` 展示了「先在索引表里 position，再回 entries 取 key 比对」的方向（[sidebar.rs:L6998-L7009](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6998-L7009)）：

```rust
fn active_project_header_position(&self, cx: &App) -> Option<usize> {
    let active_key = self.active_project_group_key(cx)?;
    self.contents
        .project_header_indices
        .iter()
        .position(|&entry_ix| {
            matches!(
                &self.contents.entries[entry_ix],
                ListEntry::ProjectHeader { key, .. } if *key == active_key
            )
        })
}
```

#### 4.3.4 代码实践

1. **实践目标**：手工模拟索引生成与消费，确认你理解「下标表 ↔ entries」的对应关系。
2. **操作步骤**：
   - 给定一个重建结果：分组 A（展开，2 线程）、分组 B（折叠，存储里有 3 线程）、分组 C（展开，0 线程、1 终端）。写出 `entries` 的变体序列与 `project_header_indices`。
   - 对照 `cycle_project_impl`：若当前活跃分组是 B，按 `NextProject` 两次分别落在哪个 key？（提示：`pos(B) → (pos+1)%3 → (pos+2)%3`。）
   - 用 `assert_project_header_has_threads`（[sidebar_tests.rs:L102-L127](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L102-L127)）的思路检验你对 `has_threads` 的判断：分组 B 折叠但存储有 3 线程，它的 `has_threads` 应为多少？
3. **需要观察的现象**：纸面推导结果与源码逻辑一致；无命令输出。
4. **预期结果**：`entries = [HeaderA, T, T, HeaderB, HeaderC, Terminal]`，`project_header_indices = [0, 3, 4]`（分组 C 展开且无线程，但终端行是它的子行；注意「No threads yet」是渲染层子行、不占下标）；`NextProject` 两次从 B → C → A；B 的 `has_threads = true`（折叠不等于没有线程，只是子行不进列表）。
5. 以上为纯源码阅读型推导，无需运行命令即可完成；如需机器验证，可在本地参照 `test_collapse_and_expand_group` 的脚手架写一个三分组测试打印 `visible_entries_as_strings`（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`project_header_indices` 为什么不缓存跨重建，而是作为 `SidebarContents` 的一部分每次重算？

**答案**：它的每个元素都指向 `entries` 的下标，而 `entries` 每次全量重建（顺序、长度都可能变）。缓存旧下标必然悬空。放进快照与 `entries` 原子地一起替换，一致性零成本——这是「派生数据不缓存」教义的直接应用。

**练习 2**：粘性头部查找用的是「≤ 滚动行号的**最后一个**下标」，而不是「≥ 滚动行号的第一个」。为什么方向必须是反向？

**答案**：粘性头部要显示的是「视口顶部已经滚出界、但它的子行仍占据视口的那个分组」的头。视口顶端的行号 `scroll_top.item_ix` 一定处于某个分组的中部（或恰好是头部），该分组的头是**不大于**它的最后一个头下标；若找「第一个 ≥ 它的头」，得到的是下一个还没滚到的分组，显示出来就张冠李戴了。

### 4.4 `render_sticky_header`：滚动联动与「推开」式吸附

#### 4.4.1 概念说明

「粘性头部」（sticky header）是长分组列表的常见交互：当你把分组 A 的头滚出视口、但仍在浏览 A 的线程时，A 的头会吸附在列表顶部，直到分组 B 的头滚上来把它「推走」。本函数在每次渲染时计算：当前该不该显示吸附头、显示哪个、以及要不要为下一个头让位而上移。

它是 `render` 的第 7764 行最先调用的渲染辅助之一（[sidebar.rs:L7764](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7764)），返回 `Option<AnyElement>`——`None` 表示不需要吸附，挂载点在列表容器的 `.when_some(sticky_header, ...)`（[sidebar.rs:L7860-L7881](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7860-L7881)）。列表容器是 `relative` 定位，吸附头 `absolute` 叠在列表之上、`no_search_results` 覆盖层之后，所以绘制在最上层。

#### 4.4.2 核心流程

算法分三步：

```text
1. 定位当前分组
   scroll_top = list_state.logical_scroll_top()
   header_idx = project_header_indices 中最后一个 ≤ scroll_top.item_ix 的下标
   （若找不到 → None；item_ix 是视口内第一行的下标，offset_in_item 是该行被滚过的像素）

2. 判断是否需要吸附
   needs_sticky = header_idx < item_ix                    ← 头已完全滚出
               || (header_idx == item_ix && offset > 0)   ← 头被滚过一部分
   否则 → None（头本身还完整可见，画吸附头反而重复）

3. 计算「推开」位移 top_offset
   next_idx = 索引表中第一个 > header_idx 的下标
   y = next 头的 bounds.origin.y − viewport.origin.y     ← 下一个头相对视口顶的 y
   h = next 头的实测高度
   若 y < h：top_offset = y − h（负值，吸附头上移让位）
   否则：top_offset = 0
```

第 3 步的判定条件写成数学形式：仅当 \( 0 \le y < h \)（下一个头的顶边已探入视口、但还没完整露出）时，吸附头向上位移 \( y - h \)（负数）；当 \( y \ge h \) 时下一个头还离得远，吸附头贴住顶部不动。效果是两个头以同一速度滑动交接，没有跳变。

另一个隐藏依赖：`bounds_for_item(next_idx)` 只对**已测量**的行返回 `Some`。这就是 u3-l3 强调「重建不得清空测量」的直接动机之一——如果重建把所有行重置为 `Unmeasured`，这里会拿不到下一个头的 bounds，`unwrap_or(px(0.))` 会让吸附头在交接瞬间错位一帧，表现为粘性头闪烁。

#### 4.4.3 源码精读

定位与吸附判定（[sidebar.rs:L3142-L3161](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3142-L3161)）：

```rust
let scroll_top = self.list_state.logical_scroll_top();

let &header_idx = self
    .contents
    .project_header_indices
    .iter()
    .rev()
    .find(|&&idx| idx <= scroll_top.item_ix)?;

let needs_sticky = header_idx < scroll_top.item_ix
    || (header_idx == scroll_top.item_ix && scroll_top.offset_in_item > px(0.));

if !needs_sticky {
    return None;
}
```

取出该行的数据后复用 `render_project_header`，注意第二个参数传 `true`（sticky id 前缀），`ix` 仍用 `header_idx`（[sidebar.rs:L3163-L3193](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3163-L3193)）：

```rust
let ListEntry::ProjectHeader { key, label, highlight_positions,
    has_running_threads, waiting_thread_count, has_notifications,
    is_active, has_threads } = self.contents.entries.get(header_idx)?
else { return None; };

let header_element = self.render_project_header(
    header_idx,
    true,        // is_sticky
    key, &label, &highlight_positions, ...
    cx,
);
```

「推开」位移（[sidebar.rs:L3195-L3207](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3195-L3207)）：`(y_in_viewport < header_height).then_some(y_in_viewport - header_height)` 一步完成了「只在交叠时上移」与「位移量 = 交叠程度」两件事：

```rust
let top_offset = self
    .contents
    .project_header_indices
    .iter()
    .find(|&&idx| idx > header_idx)
    .and_then(|&next_idx| {
        let bounds = self.list_state.bounds_for_item(next_idx)?;
        let viewport = self.list_state.viewport_bounds();
        let y_in_viewport = bounds.origin.y - viewport.origin.y;
        let header_height = bounds.size.height;
        (y_in_viewport < header_height).then_some(y_in_viewport - header_height)
    })
    .unwrap_or(px(0.));
```

最终元素：绝对定位、半透明混合背景、下边框加轻阴影（从视觉上「浮」在列表上）（[sidebar.rs:L3209-L3226](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3209-L3226)）：

```rust
let element = v_flex()
    .absolute()
    .top(top_offset)
    .left_0()
    .w_full()
    .bg(background)
    .border_b_1()
    .border_color(color.border.opacity(0.5))
    .child(header_element)
    .shadow_sm()
    .into_any_element();
```

#### 4.4.4 代码实践

1. **实践目标**：用具体数字手推吸附算法，把三步流程变成肌肉记忆。
2. **操作步骤**：
   - 设 `entries = [HeaderA(0), T1(1), T2(2), HeaderB(3), T3(4)]`，`project_header_indices = [0, 3]`，每行高 28px，视口高 100px。分别对以下三个滚动位置计算 `(header_idx, needs_sticky, top_offset)`：
     a. `item_ix = 0, offset_in_item = 0`（未滚动）；
     b. `item_ix = 1, offset_in_item = 10`（A 的头已滚出 10px）；
     c. `item_ix = 2, offset_in_item = 20`，且假设 HeaderB 的 `y_in_viewport = 6`（B 正在探入视口）。
   - 每步先按 4.4.2 的伪代码计算，再回源码核对每个中间变量对应哪一行。
3. **需要观察的现象**：纸面结果呈现「不显示 → 贴顶 → 上移让位」的三段行为，且 c 的 `top_offset = 6 − 28 = −22px`。
4. **预期结果**：a：`header_idx=0`，`0 < 0` 不成立且 offset 为 0 → `needs_sticky = false` → `None`；b：`header_idx=0 < 1` → 吸附 A，`top_offset = 0`（B 的 `y_in_viewport ≥ 28`）；c：仍吸附 A，但 `top_offset = −22px`，A 的头被 B 推上去 22px。
5. 视觉验证需真实滚动交互：在本机 `cargo run -p zed` 打开含多个线程的侧边栏并滚动（**待本地验证**）；纯逻辑验证可参照 4.2.4 的测试脚手架写一个直接调用 `render_sticky_header` 的测试，但需构造带滚动的 `ListState`，成本较高，建议以纸面推导为主。

#### 4.4.5 小练习与答案

**练习 1**：为什么判定条件要区分「头完全滚出」与「头被滚过一部分」两种情况，而不是统一用 `header_idx <= item_ix`？

**答案**：当 `header_idx == item_ix && offset_in_item == 0` 时，头正是视口第一行、完整可见，此时列表里的原位头就够了，再画吸附头等于同一行画两遍。只要 `offset_in_item > 0`，头的顶部就被裁掉了一部分，才需要吸附头补位。统一用 `<=` 会在列表顶部多出一个重复元素。

**练习 2**：吸附头的背景为什么用 `title_bar_background` 与 `panel_background` 混合（约 8:2），而不是不透明纯色？

**答案**：列表内容会从吸附头下方经过（`top_offset` 为负的交接阶段尤其明显），半透明混合让用户隐约看到被遮住的内容在移动，同时仍保证标签可读；这与 u4-l1 讲过的整体底色混合策略（标题栏色 : 面板色 ≈ 75:25）一脉相承，只是比例稍硬一点以突出「分层」。另外若窗口是透明外观，混合色能自然透出桌面背景，避免出现一块突兀的实心补丁。

**练习 3**：`render_sticky_header` 里对 `entries.get(header_idx)` 的解构失败时返回 `None`（`else { return None; }`）。什么情况下索引表指向的行不是 `ProjectHeader`？

**答案**：按生成规则不可能——索引表只在 push `ProjectHeader` 前记录下标，两者在同一个循环里同步产生并一起原子替换。这个 `else` 是防御性分支（Rust 的 `let-else` 解构枚举时必须处理另一种可能），类似 `unwrap_or(false)` 的兜底风格，不代表可到达的状态。

## 5. 综合实践

把本讲四个模块串成一条链，完成一次「折叠 → 重建 → 渲染 → 吸附」的全链路追踪：

1. **场景**：窗口里有分组 A（`/repo-a`，展开，线程 t1、t2）与分组 B（`/repo-b`，折叠，存储里有线程 t3）。写出此时的 `entries`、`project_header_indices`、每个 `EntryShape`。
2. **操作**：用户点击 B 的分组头。按顺序写出发生的一切：`on_click` 分支选择（为何不是 `activate_or_open_workspace_for_group`）→ `toggle_collapse` 三步 → `MultiWorkspace` 内部变化（含 `serialize` 做了什么）→ `rebuild_contents` 中 B 分支的新行为（`should_load_threads` 的值、通知查询、`continue`）→ 新的 `entries` / `project_header_indices` / `EntryShape` 序列 → `apply_list_state_diff` 计算出的前缀/后缀长度与 splice 区间（用 u3-l3 的算法）。
3. **验证**：把你的推导写成一张时序表，然后在本地运行：

   ```bash
   cargo test -p sidebar --lib test_collapse_and_expand_group
   cargo test -p sidebar --lib test_collapse_changes_entry_shape
   ```

   对照 `visible_entries_as_strings` 的断言输出（`v`/`>` 图标来自 4.2 的回读）修正表格。最后回答收尾问题：此刻用户把列表滚动到 B 的子区域（B 刚展开），粘性头部会显示哪个 key、`top_offset` 何时开始为负？
4. **预期结果**：一条完整的因果链：点击 → 宿主布尔翻转并落库 → 同步重建（B 的子行入列）→ 形状序列变化触发最小 splice → 视口内 B 的头吸附、A 的头在交接时被推开。若中途任何一步对不上源码，回到对应小节的「源码精读」重读。

## 6. 本讲小结

- 分组头的全部状态（标签、运行/等待/通知徽标、活跃、`has_threads`）都是重建时算进 `ListEntry::ProjectHeader` 的数据，`render_project_header` 只做纯投影；三个徽标仅在折叠时显示，是「子行状态上收门牌」的设计。
- 折叠状态存在 `MultiWorkspace.project_groups`（`{ key, expanded }` 表）而非 `Sidebar`：分组生命周期长于侧边栏可见性、序列化与 rekey 迁移都在宿主侧、侧边栏保持纯投影。读写通道是 `is_group_collapsed`（现查，缺失即展开）与 `set_group_expanded`（直改 + `serialize` 落库，不发事件，调用方自补 `update_entries`）。
- `project_header_indices` 是「分组头在 `entries` 里的下标表」，在 push header 前一行记录、随 `SidebarContents` 快照原子重建，消费方是粘性头部定位、活跃分组定位与 `NextProject`/`PreviousProject` 循环切换。
- `render_sticky_header` 三步走：反向找 ≤ 滚动行号的头下标、判定是否滚出（含部分滚出）、按下一个头的侵入量 \( y - h \) 计算推开位移；`bounds_for_item` 依赖测量缓存，这是 u3-l3 测量保留契约的直接受益者。
- 折叠同时通过两条途径改变 `EntryShape` 序列（`is_collapsed` 字段翻转 + 子行形状整段消失），`test_collapse_changes_entry_shape` 把这一点锁死为契约，保证折叠时列表正确重置受影响行的高度。

## 7. 下一步学习建议

下一讲 **u4-l3「线程行与终端行渲染」**将走进另外两种行：`render_thread` / `render_terminal` 如何拼装 `ThreadItem` 组件、状态图标与 diff 统计徽标、搜索高亮位置如何随图标前缀剥离而重新映射。本讲已铺好两块垫脚石：分组头徽标「展开时下放给子行」的约定，将在子行渲染中兑现；`highlight_positions` 从重建传到渲染的通路，也将在子行上看到更复杂的消费。若想先补上下文，可回读 u3-l3 的 `apply_list_state_diff`（本讲 4.4 的测量依赖）与 u2-l2 的 `ProjectGroupKey`（本讲全部键控状态的基座）。
