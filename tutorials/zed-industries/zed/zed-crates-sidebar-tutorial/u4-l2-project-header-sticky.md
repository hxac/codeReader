# 项目分组头与粘性头部渲染

## 1. 本讲目标

学完本讲,你应该能够:

1. 拆解 `render_project_header` 产出的「一行分组头」由哪些零件组成:标签、远程图标、折叠箭头、三个状态徽标、新建按钮、省略号菜单,以及它们各自的显示条件。
2. 解释 `is_group_collapsed` / `set_group_expanded` 这对读写方法为什么把折叠状态存放在 `MultiWorkspace` 而不是 `Sidebar` 自己身上,以及这条路径如何被序列化持久化。
3. 说明 `project_header_indices` 这个「分组头下标表」在重建时如何生成、又被哪些消费方使用。
4. 读懂 `render_sticky_header` 的吸附判定与「让位」式位移算法,理解它如何借助 `project_header_indices` 与 `ListState` 的滚动信息工作。
5. 对照 `test_collapse_and_expand_group` 与 `test_collapse_changes_entry_shape` 两个测试,说明折叠操作如何同时改变可见行集合与 `EntryShape` 序列。

## 2. 前置知识

本讲建立在 u4-l1(渲染主骨架)与 u2-l2(工作区与项目分组)之上,还需要回忆:

- **`ListEntry` 三种行**(u2-l1):侧边栏列表的每一行是 `ListEntry` 枚举的一个变体——`ProjectHeader`(项目分组头)、`Thread`(线程行)、`Terminal`(终端行)。本讲的主角是 `ProjectHeader`。
- **`ProjectGroupKey`**(u2-l2):项目分组的身份键 = 主 worktree 路径列表 + 可选的远程主机。同一窗口里 linked worktree 与主仓库共用一个键。折叠状态正是以它为键存放的。
- **`MultiWorkspace` 与弱引用宿主**(u1-l3):`Sidebar` 通过 `WeakEntity<MultiWorkspace>` 反向持有宿主(声明于 [sidebar.rs:735](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L735)),访问时必须 `upgrade()` 判空。宿主是「窗口内多项目世界模型」的拥有者。
- **全量重推导教义**(u3-l2):任何变化都汇入 `update_entries` → `rebuild_contents`,从当前世界状态**整表重算** `contents`。侧边栏自己尽量不存可推导的状态。
- **`EntryShape` 与测量保留**(u3-l3):行的「等高身份键」。相等形状必须渲染出相同高度;形状变了,`apply_list_state_diff` 才会让该区间测量失效。回忆那个关键事实:`ListState::bounds_for_item` 对未测量的行返回 `None`——本讲的粘性头部正好依赖已测量的高度。
- **gpui 虚拟列表 `list` 与 `ListState`**(u4-l1):只为视口内的行构建元素;`ListState` 缓存滚动位置与每行实测高度。粘性头部要回答的问题正是「视口顶端现在压着哪一行」。
- **FluentBuilder 条件链**:`.when(条件, |el| ...)`、`.when_some(Option, |el| 值| ...)`、`.map(|el| ...)`、`.children(Option)`——读渲染代码的四板斧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 本讲主战场:`ListEntry` 与 `SidebarContents`/`EntryShape` 定义、`is_group_collapsed`/`set_group_expanded`/`toggle_collapse`、`rebuild_contents` 中生成 `project_header_indices` 的段落、`render_list_entry`、`render_project_header`、`render_sticky_header`、`confirm` 与 `fold_all`/`unfold_all`、`cycle_project_impl` |
| `crates/workspace/src/multi_workspace.rs` | 折叠状态的真正存放处:`ProjectGroupState` 与 `project_groups` 字段、`group_state_by_key(_mut)`、`set_all_groups_expanded`、`ensure_project_group_state`、`restore_project_groups`、`serialize` |
| `crates/sidebar/src/sidebar_tests.rs` | `test_collapse_and_expand_group`、`test_collapse_changes_entry_shape`、`test_collapse_state_survives_worktree_key_change`,以及断言辅助 `visible_entries_as_strings` |
| `crates/gpui/src/elements/list.rs` | 只读两个 API 的定义:`logical_scroll_top` 返回的 `ListOffset`、`bounds_for_item`,粘性头部的数据来源 |

## 4. 核心概念与源码讲解

### 4.1 `project_header_indices`:分组头下标表

#### 4.1.1 概念说明

`rebuild_contents` 产出的 `entries` 是一个「压平」的行数组:分组头、线程行、终端行混在同一个 `Vec<ListEntry>` 里。但很多消费方需要的是「分组头在哪」这个结构信息——粘性头部要找视口顶端的分组头,项目循环切换要按分组跳转。如果每次都现场扫描 `entries` 找 `ProjectHeader` 变体,既重复又容易和渲染期状态脱节。

`SidebarContents` 因此多存了一个下标表:

- [sidebar.rs:475-482](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L475-L482) — `SidebarContents` 结构。`project_header_indices: Vec<usize>` 记录每个 `ListEntry::ProjectHeader` 在 `entries` 里的下标;`has_open_projects` 管空态。它**不是**独立状态,而是 `entries` 的派生索引——重建时与 `entries` 同步生成,永不单独修改。

分组头行本身携带九个字段(见 [sidebar.rs:391-405](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L391-L405) 的 `ListEntry::ProjectHeader`):`key`(分组键)、`label`(分组标签)、`highlight_positions`(搜索高亮)、`has_running_threads` / `waiting_thread_count` / `has_notifications`(三个汇总徽标)、`is_active`(是否当前活跃分组)、`has_threads`(组内是否有行,决定空态子行)。这些字段在重建时由 `rebuild_contents` 从元数据存储与活跃面板信息汇总写入,渲染函数只读不写。

#### 4.1.2 核心流程

`project_header_indices` 的生成时机在 `rebuild_contents` 内部,规则一句话:**先记下标,再压入头**。

```text
对每个项目分组 group:
    ……收集该组的线程与终端……
    project_header_indices.push(entries.len())   # 头即将落在这个下标
    entries.push(ListEntry::ProjectHeader { …… })
    ……压入该组的行(展开时)……
收尾:
    self.contents = SidebarContents { entries, ……, project_header_indices, …… }
```

注意两条分支都会写表:展开的组在 [sidebar.rs:1884-1894](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1884-L1894) 压入,折叠的组(行被跳过)在 [sidebar.rs:1930-1940](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1930-L1940) 压入——**折叠只影响组内行,分组头本身永远在列表里**。最终在 [sidebar.rs:1965-1971](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1965-L1971) 与 `entries` 一起装入新的 `SidebarContents`。

消费方有三处:

1. `render_sticky_header`——找视口顶端所在分组的头(4.4 节)。
2. `active_project_header_position` / `cycle_project_impl`——按分组循环切换项目。
3. 键盘导航的分组边界判定(间接,经由 `entries` 上的 `ProjectHeader` 模式匹配)。

#### 4.1.3 源码精读

**生成点(展开分支)**:[sidebar.rs:1884-1894](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1884-L1894)

```rust
project_header_indices.push(entries.len());
entries.push(ListEntry::ProjectHeader {
    key: group_key.clone(),
    label,
    highlight_positions: workspace_highlight_positions,
    has_running_threads,
    waiting_thread_count,
    has_notifications: has_thread_notifications || has_terminal_notifications,
    is_active,
    has_threads,
});
```

压入头**之前**先记录 `entries.len()`,保证下标指向头自己而不是头后第一行。三个徽标字段全部来自本组行集合的现算:`has_running_threads` / `waiting_thread_count` 由活跃面板的 `live_infos` 统计(见 [sidebar.rs:1708-1716](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1708-L1716):`Running` 置真、`WaitingForConfirmation` 计数),`has_notifications` 来自 `notified_threads` / `notified_terminals` 两个通知集合的成员检查([sidebar.rs:1877-1882](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1877-L1882))。

**消费点(项目循环切换)**:[sidebar.rs:7011-7042](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7011-L7042) 的 `cycle_project_impl`。`header_count = project_header_indices.len()` 就是分组总数;`current_pos` 由 [active_project_header_position( sidebar.rs:6998-7009)](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6998-L7009) 用「活跃分组键 == 头的 key」匹配得出;取模得到下一个分组的位置后,`project_header_indices[next_pos]` 直接换算回 `entries` 下标取出 `key`,并顺手 `set_group_expanded(&key, true, cx)` 展开目标组——这里已经能看到折叠状态的读写横跨 Sidebar 与 MultiWorkspace 两层,4.2 节展开。

#### 4.1.4 代码实践

**实践目标**:亲眼确认「折叠不删分组头,只删组内行」,以及下标表与 entries 的对应关系。

**操作步骤**(源码阅读型,无需改代码):

1. 打开 [sidebar.rs:1884](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1884) 与 [sidebar.rs:1930](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1930),对照两处 `push`,写出「同一分组在展开/折叠两种状态下各贡献几行」。
2. 阅读 [sidebar_tests.rs:949-1000](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L949-L1000) 的 `test_collapse_and_expand_group`,注意断言里折叠后只剩一行 `"  > [my-project]"`。
3. 在仓库根目录运行:

   ```bash
   cargo test -p sidebar test_collapse_and_expand_group
   ```

**需要观察的现象**:测试输出中该用例通过;断言数组从两行(`"v [my-project]"` + `"  Thread 1"`)变一行(`"> [my-project]"`)再变回两行。

**预期结果**:`ProjectHeader` 行在折叠前后都在 `entries[0]`,`project_header_indices == [0]` 不变,变化的是 `entries.len()` 从 2 变 1。`visible_entries_as_strings` 中 `>` / `v` 前缀正是测试辅助函数现场调用 `is_group_collapsed` 画出来的(见 [sidebar_tests.rs:563-570](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L563-L570))。

#### 4.1.5 小练习与答案

**练习 1**:如果一个分组被搜索过滤整组丢弃,`project_header_indices` 里还会有它的下标吗?

答案:不会。过滤发生在头压入之前——[sidebar.rs:1871-1874](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1871-L1874) 在 `matched_threads` 与 `matched_terminals` 双空且工作区不匹配时直接 `continue`,既不 `push` 下标也不 `push` 头。下标表与 `entries` 因此永远保持一致。

**练习 2**:`project_header_indices` 与 `entries` 谁是「源」?如果允许代码在重建之外单独修改 `entries`,下标表会怎样?

答案:`entries` 是源,下标表是重建期间同步产出的派生索引。若在重建之外单独改 `entries`(比如手动删一行),下标表会指向错误的行甚至越界——这正是本 crate 把 `contents` 整体替换([sidebar.rs:1965-1971](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1965-L1971))而不做增量修改的原因之一。

### 4.2 `is_group_collapsed` / `set_group_expanded`:折叠状态住在哪

#### 4.2.1 概念说明

折叠/展开是用户对「项目分组」的偏好,不是可以从世界状态推导的信息——这让它不能进 `rebuild_contents` 的重推导范畴。问题是:这个状态该存在 `Sidebar` 还是 `MultiWorkspace`?

答案已经由前几讲的原则注定:**`MultiWorkspace`**。理由有四条:

1. **归属对齐**:分组的所有权本来就在 `MultiWorkspace`——`project_groups: Vec<ProjectGroupState>`(见 [multi_workspace.rs:306-309](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L306-L309)),每个元素就是 `key + expanded`([multi_workspace.rs:284-288](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L284-L288))。`rebuild_contents` 遍历的 `MultiWorkspace::project_groups()` 快照本身就携带 `expanded` 字段,折叠与否直接决定该组要不要加载行——状态的消费者就是状态的拥有者。
2. **生命周期**:侧边栏实体可能被重建(窗口恢复时 `Sidebar::new` 重新创建),而 `MultiWorkspace` 的分组状态要活得比任何一次视图重建更久,并随窗口状态一起持久化(见下面的 `serialize`)。
3. **多消费方**:`cycle_project_impl` 切换分组时要展开目标组([sidebar.rs:7042](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7042)),`fold_all` / `unfold_all` 要批量设置全部组——这些入口都经 `MultiWorkspace` 的公开方法,而不是去摸 Sidebar 的私有字段。
4. **键迁移**:工作树增删会改变 `ProjectGroupKey`,`MultiWorkspace` 内部的 `rekey_project_group`([multi_workspace.rs:697](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L697) 起)在迁移时保留原键的 `expanded`。若状态存在 Sidebar 的 HashMap 里,换键后就成了孤儿。测试 `test_collapse_state_survives_worktree_key_change`([sidebar_tests.rs:1002](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1002) 起)专门锁死这条行为。

于是 Sidebar 只保留一对薄薄的读写门面,经弱引用宿主转发。

#### 4.2.2 核心流程

```text
读:is_group_collapsed(key)
    multi_workspace.upgrade()          # 宿主还活着吗?
      → mw.read(cx).group_state_by_key(key)
      → Some(state) ⇒ !state.expanded   # 有状态:expanded 取反
      → None / 宿主已释放 ⇒ false        # 安全默认:视为展开

写:set_group_expanded(key, expanded)
    multi_workspace.upgrade()
      → mw.update: group_state_by_key_mut(key) 改 expanded
      → mw.serialize(cx)               # 标记窗口状态需要持久化

完整交互:toggle_collapse(key)
    is_collapsed = is_group_collapsed(key)
    set_group_expanded(key, is_collapsed)   # 取反写入
    update_entries(cx)                      # 立即整表重建(不走去抖)
```

#### 4.2.3 源码精读

**读门面**:[sidebar.rs:930-939](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L930-L939)

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
```

两道防线:`upgrade()` 处理宿主实体可能已被释放(WeakEntity 的标准姿势,u1-l3);`group_state_by_key` 查不到该键(比如刚恢复序列化、组还没登记)时兜底 `false`——「默认展开」让新出现的分组不至于藏起内容。

**写门面**:[sidebar.rs:941-950](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L941-L950)

```rust
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

两个细节值得注意:`group_state_by_key_mut` 查不到键时**静默不建**(只改已存在的组状态,新组由 `MultiWorkspace::ensure_project_group_state` 在登记分组时以 `expanded: true` 创建,见 [multi_workspace.rs:670-686](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L670-L686));写完无条件调 `mw.serialize(cx)`——折叠是要记住的用户偏好,每次改动都触发宿主把整份窗口状态(含每个分组的 `expanded`)写回键值存储,见 [multi_workspace.rs:1449-1477](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1449-L1477),恢复则由 [restore_project_groups( multi_workspace.rs:825-846)](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L825-L846) 读回。

**交互入口**:[sidebar.rs:3229-3238](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3229-L3238) 的 `toggle_collapse` 把读写拼成一次取反,然后**同步**调用 `update_entries`(注意不是 `schedule_update_entries`——折叠是用户直接交互,不值得再等一拍去抖)。点击与键盘两条路都汇到这里:点击见 4.3.3,键盘 `Confirm` 在 [sidebar.rs:3528-3532](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3528-L3532)(选中行是 `ProjectHeader` 时 `Confirm` 即折叠切换)。批量折叠 `fold_all` / `unfold_all`([sidebar.rs:4341-4365](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4341-L4365))则直接借 `MultiWorkspace::set_all_groups_expanded`([multi_workspace.rs:892-896](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L892-L896))批量置位。

**与 `EntryShape` 的联动**:[sidebar.rs:2053-2071](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2053-L2071) 的 `entry_shapes` 投影形状时,**现场**查询 `multi_workspace.group_state_by_key(key)` 取 `is_collapsed`——不在 `contents` 里存副本。折叠一变,形状序列立刻不同,`apply_list_state_diff` 随之让对应区间测量失效。这正是 u3-l3 讲过的契约在分组头上的体现:`EntryShape::ProjectHeader` 之所以同时含 `has_threads` 与 `is_collapsed`([sidebar.rs:489-496](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L489-L496)),是因为这两个布尔共同决定头下面会不会多出一行「No threads yet」(见 4.3.3),直接影响该行高度。

#### 4.2.4 代码实践

**实践目标**:亲手验证「折叠状态存在 MultiWorkspace、随 serialize 持久化、键迁移不丢」三件事,并解释设计原因。

**操作步骤**:

1. 通读 [sidebar.rs:930-950](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L930-L950) 与 [multi_workspace.rs:879-896](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L879-L896),确认读写最终都落在 `ProjectGroupState.expanded` 一个布尔上。
2. 运行两个测试:

   ```bash
   cargo test -p sidebar test_collapse_and_expand_group
   cargo test -p sidebar test_collapse_changes_entry_shape
   ```

3. 阅读 [multi_workspace.rs:1449-1477](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1449-L1477) 的 `serialize`,找到 `group.expanded` 被写进 `MultiWorkspaceState` 的那一行;再读 [sidebar_tests.rs:753-783](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L753-L783) 的 `test_serialization_round_trip`(它在序列化前特意 `toggle_collapse` 折叠了分组),确认折叠状态走的是宿主的持久化通道而非 Sidebar 自己的。
4. 用自己的话写 3-5 句:为什么折叠状态若存在 `Sidebar` 上,`cycle_project_impl`、窗口恢复、worktree 换键三个场景会各自出什么问题。

**需要观察的现象**:两条测试命令均通过;`serialize` 的闭包里能指出 `project_groups` 映射进持久化状态的确切位置。

**预期结果**:你能指出——`cycle_project_impl` 将无法展开目标组(Sidebar 私有字段对其他组件不可见);窗口恢复重建 Sidebar 后折叠丢失(状态没有进宿主的序列化);换键后 `rekey_project_group` 迁移的是 MultiWorkspace 自己的表,Sidebar 侧的旧键条目变孤儿。

#### 4.2.5 小练习与答案

**练习 1**:`is_group_collapsed` 的兜底值是 `false`(展开)。如果改成 `true`(折叠),哪些场景会表现异常?

答案:凡是「分组尚未登记就渲染」的路径都会把组画成折叠:例如刚恢复序列化、`restore_project_groups` 还没跑完时的首帧;或新项目刚加入、`ensure_project_group_state` 尚未创建状态。用户会看到内容「闪没了」再展开。默认展开是更安全的中性初值。

**练习 2**:`toggle_collapse` 里调用的是 `update_entries(cx)` 而不是 `schedule_update_entries(...)`。结合 u3-l2 的去抖机制,说说为什么不合并。

答案:去抖的意义是把事件风暴(元数据批量写入、多工作区事件)合并成一次重建。折叠是单次用户点击,没有可合并的对象;且用户期望点击后**立即**看到行消失。同步 `update_entries` 省掉一拍延迟。反过来,若是 `WorktreePathsChanged` 这类可能连续触发的事件,走 `schedule_update_entries` 才划算。

**练习 3**:`entry_shapes` 为何现场查 `MultiWorkspace` 而不在 `SidebarContents` 里存一份 `is_collapsed`?

答案:现场查询保证形状序列与真实折叠状态**始终一致**——不存在「contents 里的副本过期」这一类 bug;也符合「能现查的不要缓存」的重推导教义。代价只是每次投影多一次 `Vec` 线性查找,而分组数通常是个位数。

### 4.3 `render_project_header`:一行分组头的零件清单

#### 4.3.1 概念说明

`render_project_header` 是分组头的唯一渲染函数,**同时**服务两个调用方:列表内的真实行,和浮在列表上方的粘性副本(4.4 节)。它通过 `is_sticky: bool` 参数区分二者,主要影响元素 ID 前缀——同一时刻两份拷贝可能同时存在于元素树中,ID 必须不同。

这行头的职责可以概括为「门牌 + 仪表盘 + 三个入口」:

- **门牌**:分组标签(搜索时带高亮)、可选的远程项目图标。
- **仪表盘**:折叠时才显示的三个状态徽标——运行中(旋转加载圈)、等待确认(警告图标 + 数量提示)、有通知(强调色圆点)。设计动机:展开时每行的状态行内可见,头不需要汇总;折叠后行都藏了,头必须替它们说话。
- **三个入口**:点击整行切换折叠(副键点击则是激活该工作区)、右侧「+」新建线程按钮、省略号菜单(右键或点开)。

#### 4.3.2 核心流程

```text
render_project_header(ix, is_sticky, key, label, highlight_positions,
                      has_running_threads, waiting_thread_count,
                      has_notifications, is_active, is_focused, has_threads)
  ├─ 查 is_group_collapsed(key) ⇒ 折叠箭头方向(ChevronRight / ChevronDown)
  ├─ id_prefix = is_sticky ? "sticky-" : ""   ⇒ 所有元素 ID / group 名加前缀防撞
  ├─ 标签:有高亮位置用 HighlightedLabel,否则 Label;
  │    非活跃分组淡色;透明窗口改为截断(渐变遮罩在透明背景上会显形)
  ├─ 左半区:标签 + 远程图标 + [折叠时的三个徽标] + [悬停可见的箭头]
  ├─ 右半区:渐变遮罩 + 新建按钮 + 省略号菜单(鼠标按下即吞掉,不触发折叠)
  ├─ on_click:副键 ⇒ 激活该组工作区;普通点击且有搜索词 ⇒ 不动作;否则 toggle_collapse
  └─ 收尾:!is_collapsed && !has_threads ⇒ 头下面再垫一行 "No threads yet" 空态子行
```

搜索状态下(`has_filter`)交互被刻意收敛:不显示手型光标、无悬停背景、不画折叠箭头、点击不切换折叠——过滤把列表拍平成分组混合视图,折叠语义此时不适用。

#### 4.3.3 源码精读

**入口与两处调用点**。函数签名在 [sidebar.rs:2259-2273](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2259-L2273)。列表内真实行的调用在 `render_list_entry`([sidebar.rs:2186-2216](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2186-L2216)),`is_sticky` 传 `false`,并顺手为该行惰性登记两个菜单句柄(`project_header_menu_handles` / `project_header_new_thread_menu_handles`,字段声明见 [sidebar.rs:776-777](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L776-L777),详情留到 u4-l4)。粘性副本的调用在 [sidebar.rs:3180-3193](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3180-L3193),`is_sticky` 传 `true`。

**ID 前缀防撞**:[sidebar.rs:2278-2287](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2278-L2287)

```rust
let id_prefix = if is_sticky { "sticky-" } else { "" };
let id = SharedString::from(format!("{id_prefix}project-header-{ix}"));
let group_name = SharedString::from(format!("{id_prefix}header-group-{ix}"));
let is_collapsed = self.is_group_collapsed(key, cx);
let disclosure_icon = if is_collapsed { IconName::ChevronRight } else { IconName::ChevronDown };
```

折叠状态是**渲染期现查**的——又一次「不存副本」。`group_name` 供悬停显隐(`visible_on_hover`)与渐变遮罩定位使用。

**折叠时才出现的三个徽标**:[sidebar.rs:2362-2400](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2362-L2400)。整段包在 `.when(is_collapsed, ...)` 里:运行中是带旋转动画的 `LoadCircle`;等待确认是 `Warning` 图标加单复数处理的 tooltip("1 thread is waiting..." / "N threads are waiting...");通知圆点只在**既没运行也没等待**时显示(`has_notifications && !has_running_threads && waiting_thread_count == 0`),避免三个徽标叠罗汉。展开时这些信息由组内每行的状态承担,头保持安静。

**点击行为**:[sidebar.rs:2444-2452](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2444-L2452)

```rust
.on_click(cx.listener(move |this, event: &gpui::ClickEvent, window, cx| {
    if event.modifiers().secondary() {
        this.activate_or_open_workspace_for_group(&key_for_focus, window, cx);
    } else if !this.has_filter_query(cx) {
        this.toggle_collapse(&key_for_toggle, window, cx);
    }
}))
```

三级分流:副键(如 Cmd+点击)激活/打开该组的工作区;搜索中主键点击不动作;否则才是折叠切换。右侧按钮区自己先 `on_mouse_down` 里 `cx.stop_propagation()`([sidebar.rs:2429-2431](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2429-L2431)),防止点「+」误折叠;右键则直接弹出省略号菜单([sidebar.rs:2433-2443](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2433-L2443))。

**空态子行**:[sidebar.rs:2455-2477](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2455-L2477)。`!is_collapsed && !has_threads` 时,函数返回的不是头本身,而是「头 + 占位圆点 + "No threads yet" 小字」的竖排容器。这一行子行正是 `EntryShape::ProjectHeader` 必须包含 `has_threads` 与 `is_collapsed` 两个布尔的原因:它们联立决定这个条目的**总高度**。

**相邻分组的分隔线**:`render_list_entry` 收尾时,第二个及以后的分组头会包一层顶边框([sidebar.rs:2223-2229](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2223-L2229)),让分组之间有视觉界线。

#### 4.3.4 代码实践

**实践目标**:把 11 个输入参数与产出的 UI 零件一一对应,并理解「同函数两份拷贝」。

**操作步骤**:

1. 在 [sidebar.rs:2259-2273](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2259-L2273) 抄下参数表,给每个参数标注:它影响哪个零件、来自哪个数据源(例如 `waiting_thread_count` ← `rebuild_contents` 统计的 `live_infos`,`is_focused` ← 渲染期 `focus_handle.is_focused(window)`)。
2. 本地实验(可复原的小改动):把 [sidebar.rs:2283-2287](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2283-L2287) 的 `disclosure_icon` 两个值对调(ChevronRight ⇄ ChevronDown),运行 `cargo test -p sidebar test_collapse_and_expand_group`,观察是否仍然通过;再运行 Zed(`cargo run -p zed`)肉眼确认箭头方向反了。看完记得还原。

**需要观察的现象**:测试通过——`visible_entries_as_strings` 画的 `>` / `v` 来自 `is_group_collapsed` 而非真实图标,图标换向不影响断言;但真实 UI 里折叠方向与直觉相反。

**预期结果**:认识到「测试断言的是状态与行集合,不是像素」;同时确认 `render_project_header` 的图标只是状态的投影。若你没有本地运行环境,此步标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**:为什么通知圆点的条件是 `has_notifications && !has_running_threads && waiting_thread_count == 0`?

答案:三个徽标共享头右侧的同一小块空间。运行圈与等待警告表达「正在发生的事」,信息量更大;通知点只是「有新动静」。当运行或等待已占据位置时省略通知点,用优先级换密度,避免视觉堆叠。

**练习 2**:透明窗口上标签改用 `.truncate()` 而非渐变遮罩([sidebar.rs:2292-2307](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2292-L2307) 与 [sidebar.rs:2413](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2413) 的 `children(opaque_window.then(|| gradient_overlay()))`)。为什么?

答案:渐变遮罩本质是「从不透明背景色渐变到透明」的色块,依赖背景是实色才看不出来。透明窗口背景是半透明的,叠上去会显出一块可见的补丁。截断不需要背景色参与,所以在非 Opaque 外观下用截断兜底。

**练习 3**:粘性副本与列表内真实行同时渲染同一条目,除了 ID 前缀还需要注意什么?

答案:交互一致性——两份拷贝都能折叠、都能开菜单。粘性头带着同样的 `on_click` 折叠逻辑与菜单句柄(句柄表按 `ix` 索引,两份拷贝共用同一 `ix` 的句柄,菜单弹出位置跟随实际点击的那份)。ID 前缀只解决元素树撞名,状态本身(`is_collapsed`)两份都是现查的,天然一致。

### 4.4 `render_sticky_header`:吸附判定与让位位移

#### 4.4.1 概念说明

列表滚动后,当前分组的头会被滚出视口,用户便失去「我现在在哪个项目里」的参照。粘性头部(sticky header)解决它:把视口顶端所在分组的头**复制一份**,以绝对定位浮在列表上方,滚动到哪跟到哪——iOS 分节列表的section header是同款交互。

实现要回答三个问题:

1. **该显示哪个分组的头?** 用 `project_header_indices` + 滚动位置定位。
2. **什么时候显示?** 真实行还看得见就不需要副本。
3. **下一个分组的头滚上来时怎么办?** 让当前副本向上滑出,完成「交接棒」。

前两个问题靠 `ListState::logical_scroll_top()`——它返回 `ListOffset { item_ix, offset_in_item }`(定义见 [list.rs:1430-1438](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/list.rs#L1430-L1438)),即「视口顶边逻辑上压在第几行、往下偏移多少像素」。第三个问题靠 `bounds_for_item`(见 [list.rs:711](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/list.rs#L711))拿下一分组头的实测边界做推挤计算——这正是 u3-l3 强调「必须保留测量值」的原因:若测量被重置,`bounds_for_item` 返回 `None`,让位位移直接失灵。

#### 4.4.2 核心流程

```text
render_sticky_header(window, cx) → Option<AnyElement>
  1. scroll_top = list_state.logical_scroll_top()      # (item_ix, offset_in_item)
  2. header_idx = project_header_indices 中最后一个 ≤ scroll_top.item_ix 的下标
       (没有 ⇒ None,视口顶边还在第一个分组头之前)
  3. needs_sticky = header_idx < scroll_top.item_ix
                    || (相等 且 offset_in_item > 0)
       (真实头完全可见 ⇒ false ⇒ None)
  4. 取 entries[header_idx] 的 ProjectHeader 字段,
     调 render_project_header(header_idx, is_sticky = true, ...)
  5. top_offset(让位位移):
       next_idx = project_header_indices 中第一个 > header_idx 的下标(下一分组头)
       bounds = list_state.bounds_for_item(next_idx)     # None ⇒ 不位移
       y = bounds.origin.y - viewport.origin.y            # 下一头在视口内的纵坐标
       header_height = bounds.size.height
       当 y < header_height 时: top_offset = y - header_height(负值,向上顶出)
       否则 top_offset = 0
  6. 返回 absolute 定位、top(top_offset) 的浮层元素
```

让位位移的几何意义:记下一分组头在视口内的纵坐标为 \( y \),其高度为 \( h \)。当 \( y \ge h \) 时它还够不着粘性区,副本稳坐 \( 0 \);一旦 \( y < h \),副本的顶部被设为

\[ \text{top\_offset} = y - h < 0 \]

即副本开始**向上滑出视口**,滑出的节奏与下一分组头推进的节奏完全同步;当下一分组头彻底顶上来、`scroll_top.item_ix` 越过它时,第 2 步的 `header_idx` 前移,副本换成新分组的头,完成交接。

#### 4.4.3 源码精读

**定位与吸附判定**:[sidebar.rs:3142-3161](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3142-L3161)

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

`.rev().find(...)` 取「最后一个不超过滚动顶行」的头——因为 `project_header_indices` 天然升序,这就是视口顶端所在分组的头。`needs_sticky` 的两个条件分别覆盖「头已被完整滚过」与「头滚了一半」;头恰好完整可见时不显示副本,避免双影。

**复用渲染 + 让位计算**:[sidebar.rs:3163-3207](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3163-L3207)。解构出 `ProjectHeader` 的字段后原样喂给 `render_project_header(..., true, ...)`,副本因此与真实行像素级一致。位移段逐句对应上面流程第 5 步:

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

一个实现细节:阈值 `header_height` 取的是**下一个头**的实测高度,而非粘性副本自身的高度——各分组头等高(`Tab::content_height`),用实测值可以顺带吸收空态子行等带来的高度差异;取不到边界(行未渲染或未测量)时一律不位移,宁可不让位也不跳变。

**浮层与挂载点**:[sidebar.rs:3214-3224](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3214-L3224) 产出 `v_flex().absolute().top(top_offset)` 的容器,带半透明混色背景、底边框与浅阴影(阴影是「浮在上面」的视觉暗示)。它由 `render()` 在构建树的最早时刻算出([sidebar.rs:7764](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7764)),随后作为**绝对定位子元素**叠在包裹 `list` 的 `relative` 容器里([sidebar.rs:7859-7875](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7859-L7875))——叠在列表之上、无结果提示之下。

#### 4.4.4 代码实践

**实践目标**:用具体数字走一遍吸附判定与让位计算,确认你理解算法而非仅仅眼熟代码。

**操作步骤**(纸面推演 + 测试验证):

1. 设想 `entries` 为 8 行、两个分组:`project_header_indices = [0, 5]`,每行高 30px(头同样 30px,无空态子行)。分别对下列滚动位置写出 `header_idx`、`needs_sticky`、粘性头显示哪个分组:
   - `scroll_top = (0, 0px)`
   - `scroll_top = (1, 0px)`
   - `scroll_top = (0, 12px)`
   - `scroll_top = (6, 10px)`
2. 接上题:当第二个分组头(下标 5)恰好滚到视口内纵坐标 \( y = 20\text{px} \) 时,计算 `top_offset`。
3. 运行防回归测试确认测量保留机制在护着这条路:

   ```bash
   cargo test -p sidebar test_thread_metadata_update_preserves_sticky_header_measurements
   ```

**需要观察的现象/预期结果**:四小题答案依次为——(0, false, 无)真实头完整可见;`header_idx = 0` 但 `item_ix = 1 > 0` ⇒ (0, true, 第一组);`item_ix = 0` 且 `offset_in_item = 12 > 0` ⇒ (0, true, 第一组,头滚了一半);`.rev().find(≤6)` 命中 5 ⇒ (5, true, 第二组)。让位题:\( y = 20 < h = 30 \),`top_offset = 20 - 30 = -10\text{px} \),副本上移 10px 让位。测试通过说明重建不会丢掉这些计算依赖的测量值。

#### 4.4.5 小练习与答案

**练习 1**:为什么定位用 `.rev().find(idx <= scroll_top.item_ix)` 而不是正序找第一个大于的再减一?

答案:等价,但逆序直取更直接且天然处理「滚动顶行在第一个头之前」的情形——此时找不到任何满足条件的下标,`?` 直接返回 `None`,无需正序版本的边界特判。

**练习 2**:如果 `apply_list_state_diff` 被改成「每次重建全部置为 Unmeasured」,粘性头部会出什么具体症状?

答案:`bounds_for_item(next_idx)` 大概率返回 `None`(未测量的行没有边界),让位位移恒为 0;滚动穿过分组边界时旧副本不再滑出、新副本瞬间替换,出现「跳变」而非「交接」;配合 u3-l3 讲过的粘性头闪跳,整体表现为分组交界处的视觉抖动。

**练习 3**:粘性头部在 `render()` 里只计算一次([sidebar.rs:7764](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7764))。滚动本身会触发重新计算吗?

答案:会。gpui 的 `list` 滚动由 `ListState` 跟踪,滚动改变视口即标记所属元素脏,宿主实体随之重渲染;重渲染进入 `render()` 时用**最新**的 `logical_scroll_top()` 与 `bounds_for_item` 重算副本与位移。也就是说粘性效果不需要独立的滚动订阅,它搭了渲染循环的便车。

## 5. 综合实践

**任务:写一份《折叠一次分组,系统里发生了什么》的完整时序说明**。

以「用户点击分组头」为起点,把本讲四个最小模块串成一条链,要求每一步都给出源码位置:

1. **点击命中**:`render_project_header` 的 `on_click` 分流([sidebar.rs:2444-2452](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2444-L2452)),普通点击且无搜索词 ⇒ `toggle_collapse`。
2. **状态写入**:`toggle_collapse`([sidebar.rs:3229-3238](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3229-L3238))→ `is_group_collapsed` 读 → `set_group_expanded` 写 `ProjectGroupState.expanded` 并触发 `mw.serialize`([sidebar.rs:941-950](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L941-L950)、[multi_workspace.rs:1449-1477](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1449-L1477))。
3. **整表重建**:同步 `update_entries` → `rebuild_contents` 走折叠分支,组内行不再加载,但 `project_header_indices` 照常登记头([sidebar.rs:1930-1940](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1930-L1940)),头上的三个徽标从此开始工作。
4. **测量差异**:重推导后的 `entry_shapes` 中该头的 `is_collapsed` 翻转([sidebar.rs:2053-2071](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2053-L2071)),`apply_list_state_diff` 据此 splice,变化的行失去测量、未变的行保留。
5. **渲染验证**:`render_project_header` 现查 `is_group_collapsed` 换箭头方向并点亮徽标;若用户正滚在列表中间,`render_sticky_header` 的副本也随之换脸。

然后运行三个测试作为证据链:

```bash
cargo test -p sidebar test_collapse_and_expand_group
cargo test -p sidebar test_collapse_changes_entry_shape
cargo test -p sidebar test_collapse_state_survives_worktree_key_change
```

三个测试分别锁定「可见行集合正确」「形状序列变化从而测量正确失效」「键迁移后折叠存活」三个环节。若本机暂不能运行,标注「待本地验证」并保留纸面时序。

## 6. 本讲小结

- `SidebarContents.project_header_indices` 是与 `entries` 同步生成的派生下标表:重建时「先记下标再压头」,折叠只删组内行、不删头;消费方是粘性头部与项目循环切换。
- 折叠状态(`ProjectGroupState.expanded`)住在 `MultiWorkspace` 而非 `Sidebar`:分组所有权、序列化持久化、多消费方(`cycle_project_impl`、`fold_all`)与键迁移保留四条理由共同决定;Sidebar 只留 `is_group_collapsed` / `set_group_expanded` 一对薄门面,写入即 `serialize`。
- `render_project_header` 一个函数服务列表内真实行与粘性副本两份拷贝,靠 `is_sticky` 前缀防 ID 撞车;折叠时点亮「运行/等待/通知」三个徽标替被藏起的行说话;`!is_collapsed && !has_threads` 时垫出 "No threads yet" 子行——这正是 `EntryShape::ProjectHeader` 必须含 `has_threads` 与 `is_collapsed` 的原因。
- `render_sticky_header` 用 `logical_scroll_top` + 下标表定位视口顶端分组,用「头被滚过或滚了一半」判定是否显示,用下一分组头的实测边界做负向 `top_offset` 让位交接;整套机制依赖 u3-l3 的测量保留契约。
- 折叠交互三入口汇一流:点击与键盘 `Confirm` 都进 `toggle_collapse`,批量走 `set_all_groups_expanded`;搜索态下折叠交互整体禁用。

## 7. 下一步学习建议

下一讲 u4-l3《线程行与终端行渲染》顺着 `render_list_entry` 的另外两个分支往下读:`render_thread` 与 `render_terminal` 如何拼装 `ThreadItem` 组件、状态图标与 diff 统计徽标,以及 `split_leading_icon_char` / `pick_icon_glyph` 的标题前缀图标化。届时你会发现本讲分组头上的三个「汇总徽标」与线程行上的「个体状态」是一对互补设计。若想先巩固本讲,建议回头精读 [multi_workspace.rs:697](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L697) 起的 `rekey_project_group`,弄清键冲突时「活跃工作区的组获胜」规则如何保住折叠状态。
