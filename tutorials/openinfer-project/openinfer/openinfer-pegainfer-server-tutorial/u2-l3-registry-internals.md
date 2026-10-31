# u2-l3 ModelLineRegistry：注册表与 CLI 归属自检

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `RegistryClaims` 用「两本账」（flag id 账 + CLI 拼写账）在注册表构造期查出所有 id 与拼写冲突，并说出为什么这套 `panic!` 自检在 release 构建里也必须存在（clap 的等价 schema 断言在 release 被禁用）。
2. 走查 `ModelLineRegistry::new` 的构造期自检流水线：共享 flags 先入账 → `CORE_ARGS` 核对 → 逐线登记（线名唯一、`consumed_shared_args` 必须指向真实共享 id、独占 flag 入账）→ 依赖表统一校验。
3. 走查 `detect` 的收集逻辑：`rejections` 列表如何逐线拼进 `DetectError::NoMatch`、两条线同时认领时 `DetectError::Conflict` 如何点名双方。
4. 理解 `validate_provided` 的 consume-or-reject：每个显式提供的 flag 必须是核心 flag、被检测线消费的共享 flag、或该线独占 flag 三者之一，否则得到 `CliError::UnconsumedFlag`，且错误消息文本本身就是 API。
5. 会用公开的 `parse_for_line` 测试帮手在模型 crate 的单元测试里复现整条「解析 + 校验」链，并说清它建的是**单线**注册表、因此哪些错误它永远报不出来。

本讲承接 u2-l1（`ModelLine` trait 八方法的分工）与 u2-l2（三层 flag 体系与 `provided_args` 的「出身」语义），把视角从「单条线长什么样」切换到「注册表如何管住所有线」。

## 2. 前置知识

### 2.1 承接前两讲的结论

- u2-l1：server 二进制只持有一列 `&'static dyn ModelLine`，从不 import 任何模型 crate 的选项类型；接入一条新线 = 实现 trait + 注册一行 + 一个 Cargo feature。
- u2-l2：flag 分三层——核心 flag（`CORE_ARGS`）、共享 flag（`SharedArgs` 其余部分，消费权由各线 `consumed_shared_args` 声明）、线独占 flag（经 `augment_cli` 挂上）；`provided_args` 借 clap 的 `ValueSource` 区分「用户显式提供」与「默认值填充」。

本讲要回答的问题随之而来：**这么多线、这么多 flag 拼在同一个 CLI 里，谁来保证它们互不打架？谁来判定用户敲下的某个 flag 归谁管？** 答案就是 `ModelLineRegistry`。

### 2.2 clap 的对象模型（本讲用到的部分）

| 概念 | 含义 | 例子 |
|------|------|------|
| `clap::Command` | 一次 CLI 的完整定义（持有全部 Arg） | `clap::Command::new("pegainfer")` |
| `clap::Arg` | 单个 flag 的定义 | `Arg::new("tp_size").long("tp-size")` |
| id | flag 的**程序内标识**，代码用 id 从 `ArgMatches` 取值 | `"tp_size"` |
| long / short | 用户输入的**拼写** | `--tp-size` / `-q` |
| alias / short_alias | 长别名 / 短别名，同一 arg 的第二拼写 | `.alias("tp-size")` |
| `clap::ArgMatches` | 一次解析的结果 | `matches.value_source(id)` |

关键区分：**id 是给程序看的，拼写是给用户敲的**。同一个冲突可以发生在两个层面——两条线的 flag 撞 id（程序内取值二义），或撞拼写（用户输入无法路由到唯一 arg）。后面会看到注册表为此维护了**两本独立的账**。

### 2.3 `assert!` 与 `debug_assert!`：release 构建的差异

Rust 的 `debug_assert!` 只在 debug 构建里编译进二进制，release 下被整个剔除；`assert!` 与 `panic!` 在任何优化级别都保留。clap 在构建/解析 `Command` 时带有一批 schema 一致性断言（例如参数定义冲突），这类检查属于 debug-only——源码注释的措辞是「Clap's equivalent schema assertions are disabled in release builds」。

而 PegaInfer 的服务端二进制**始终以 `--release` 发布**（见 u1-l2：GPU 构建必须 `--release`）。这意味着 clap 的那道检查在生产二进制里不存在，注册表必须**自己**用 `assert!`/`panic!` 把关。这就是 `ModelLineRegistry::new` 存在的核心理由。

### 2.4 panic 与 Err 的分工（承接 u1-l3 的「失败形态」）

| 失败种类 | 机制 | 面向谁 | 何时发生 |
|----------|------|--------|----------|
| 注册表自身缺陷（撞 id / 撞拼写 / 依赖表悬空） | `panic!` / `assert!` | 程序员（改代码） | 进程启动构造注册表时 |
| 用户 CLI 与检测到的线不匹配 | `thiserror` 错误（`CliError` / `DetectError`） | 用户（改命令行） | 每次启动解析之后 |

一句话：**构造期的 panic 管「代码写错了」，运行期的 Err 管「命令敲错了」**。

### 2.5 `BTreeMap` / `BTreeSet`

有序映射 / 有序集合。注册表用它们做「键 → 唯一 owner」的查账与去重，同时保证遍历顺序确定（错误消息稳定、测试可逐字断言）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [../pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L1-L987) | 本讲主战场：`RegistryClaims`、`ModelLineRegistry`、`parse_for_line` 及其测试模块全部在此 |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L190) | 注册表的三处消费点：构造、detect 接线、validate_provided 调用 |

本讲的源码引用集中在 `model_line.rs` 一个文件里，链接均指向当前 HEAD（`af9e40e1`）。以下用相对行号引用时不再重复说明。

## 4. 核心概念与源码讲解

### 4.1 两本账与一位断言官：RegistryClaims

#### 4.1.1 概念说明

`RegistryClaims` 是注册表的「查账员」。它维护两本账：

- **id 账**：flag 的程序内标识 → 拥有者（owner）；
- **拼写账**：用户敲的 CLI 拼写（`--long`、`-s`，含全部别名）→ 拥有者。

拥有者 `ArgOwner` 只有两类：`Shared`（`SharedArgs`）或 `Line(线名)`（某条模型线）。每个 flag 入账时先查账：同一键已被别人登记就 `panic!`——这是**构造期**的自检，不等用户输入。

用集合语言表述构造完成后的不变式：设 \(\mathcal{I}\) 为全部 flag id、\(\mathcal{S}\) 为全部 CLI 拼写、\(\mathcal{O} = \{\mathrm{Shared}\} \cup \{\mathrm{Line}(n)\}\)，则两本账各自构成良定义的映射

\[
\mathrm{owner}:\; \mathcal{I} \cup \mathcal{S} \rightharpoonup \mathcal{O}, \qquad
\forall k,\; \bigl|\mathrm{owner}(k)\bigr| \le 1 ,
\]

即任何键至多一个拥有者。「查账 + panic」就是在维护这个单值性。

#### 4.1.2 核心流程

一个 flag 入账（`register_arg`）的伪代码：

```text
对 command 里的每个 arg:
    id = arg.id                 → 记入 id 账（撞车即 panic，见下）
    对 arg.long 及其全部长别名:  → 记入拼写账，键为 "--<拼写>"
    对 arg.short 及其全部短别名: → 记入拼写账，键为 "-<字符>"
```

id 撞车的 panic 有两个分支：

```text
若 id 已被登记:
    若先前 owner 是 Shared 且新 owner 是某条线:
        panic "model line <线名> redeclares SharedArgs flag id <id>;
               list it in consumed_shared_args instead"   # 指向性消息
    否则:
        panic "<先前 owner> and <新 owner> both define flag id <id>"
```

拼写撞车只有一个分支：`panic "<先前 owner> and <新 owner> both define CLI spelling <拼写>"`。

此外 `validate_requirement` 校验依赖表（`arg_requirements` / `SHARED_ARG_REQUIREMENTS`）的三条断言：source 必须属于声明者自己；flag 不能要求自己；每个 target 必须是 Shared 或同一 owner 的已登记 id。

#### 4.1.3 源码精读

`RegistryClaims` 的定义就两个字段——两本账：

> [../pegainfer-frontend/src/model_line.rs:405-409](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L405-L409)
> 注册表的两本账：`ids` 与 `spellings`，值都是 `ArgOwner`。这段是数据结构声明。

`ArgOwner` 及其 `Display` 实现——panic 消息里的主体名就来自这里：

> [../pegainfer-frontend/src/model_line.rs:390-403](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L390-L403)
> `ArgOwner::Shared` 渲染为 `"SharedArgs"`，`ArgOwner::Line(name)` 渲染为 `"model line <name>"`，使 panic 消息能直接点名双方。

`register_arg` 把一个 arg 的 id、长拼写（含 `alias`）、短拼写（含 `short_alias`）全部入账：

> [../pegainfer-frontend/src/model_line.rs:419-437](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L419-L437)
> 注意 `get_long()` 与 `get_all_aliases()` 链在一起、`get_short()` 与 `get_all_short_aliases()` 链在一起——别名与主拼写进**同一本**拼写账。

`claim_id` 的双分支 panic：

> [../pegainfer-frontend/src/model_line.rs:439-451](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L439-L451)
> 线重新声明了一个共享 id 时给出专属消息「list it in consumed_shared_args instead」——因为正确修法不是给 flag 改名，而是把它列进 `consumed_shared_args`（即声明「我消费这个共享 flag」而不是自己再造一个）。

`claim_spelling` 与 `validate_requirement`：

> [../pegainfer-frontend/src/model_line.rs:453-458](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L453-L458)
> 拼写撞车的唯一 panic 分支；拼写键自带 `--` / `-` 前缀（在 `register_arg` 里 `format!` 拼上）。
>
> [../pegainfer-frontend/src/model_line.rs:464-477](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L464-L477)
> 依赖表三断言：source 属于声明者、不自指、target 属于 Shared 或同一 owner。注意这里全部用 `assert!`（release 也保留），而不是 clap 的属性宏——`arg_requirements` 的 trait 文档明说了动机：「Declare them here, not in Clap attributes, so release builds validate every referenced id」（[../pegainfer-frontend/src/model_line.rs:357-361](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L357-L361)）。

测试模块里五个 `#[should_panic]` 用例把每种撞车都锁住了，例如同拼写不同 id 的 `LINE_LONG_COPYCAT`：

> [../pegainfer-frontend/src/model_line.rs:787-797](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L787-L797)
> `LINE_LONG_COPYCAT` 的 id 是 `different_id`、long 拼写是 `line-a-flag`——id 不撞、拼写撞，专门证明「两本账缺一不可」。
>
> [../pegainfer-frontend/src/model_line.rs:934-938](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L934-L938)
> 对应测试 `registry_rejects_duplicate_exclusive_flag_spellings`，期望 panic 子串 `both define CLI spelling "--line-a-flag"`。

#### 4.1.4 代码实践

1. **实践目标**：确认五种构造期撞车都会被 `ModelLineRegistry::new` 的 panic 拦下，并把每条 panic 消息对回 4.1.2 的格式串。
2. **操作步骤**：
   ```bash
   # 单元测试无需 GPU（pegainfer-frontend 是纯 Rust crate，见其 Cargo.toml）
   cargo test --release -p pegainfer-frontend --lib model_line
   ```
   重点看这五个用例：`registry_rejects_duplicate_exclusive_flag_ids`、`registry_rejects_duplicate_exclusive_flag_spellings`、`registry_rejects_shared_flag_id_collisions`、`registry_rejects_shared_flag_alias_collisions`、`registry_rejects_short_alias_collisions`（[../pegainfer-frontend/src/model_line.rs:928-960](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L928-L960)）。想单看 panic 现场，可加 `-- --nocapture` 并临时注释 `#[should_panic]`（在自己工作副本上做，勿提交）。
3. **需要观察的现象**：`#[should_panic(expected = "...")]` 的 expected 子串与 4.1.3 引用的格式串逐段吻合；例如短别名用例期望 `model line LineShortA and model line LineShortAlias both define CLI spelling "-q"`。
4. **预期结果**：`model_line` 模块全部测试通过（含五个 panic 用例与若干正常路径用例）。
5. 本讲义写作环境未获准执行该命令，运行输出**待本地验证**；panic 消息原文均直接引自源码格式串与测试断言（[../pegainfer-frontend/src/model_line.rs:946-960](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L946-L960)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 id 和拼写要分两本账，一本不行吗？
**答案**：id 是程序内取值的键（`ArgMatches` 按 id 取值），拼写是用户输入的表面形式，两者可以独立撞车。`LINE_A_COPYCAT`（[../pegainfer-frontend/src/model_line.rs:776-786](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L776-L786)）证明同 id 可撞（拼写不同），`LINE_LONG_COPYCAT`（L787-797）证明同拼写可撞（id 不同）。只查一本账就会漏掉另一类冲突，前者导致运行期取值二义，后者导致用户输入无法路由。

**练习 2**：`claim_id` 为什么给「线重声明共享 id」开一个专属 panic 分支，而不是用通用消息？
**答案**：因为修复路径不同。两条线撞 id 时改名字即可；而线声明了与 `SharedArgs` 同名的 id 时，正确动作通常是**删掉自己的定义、把该 id 列入 `consumed_shared_args`**（声明「我消费这个共享 flag」）。消息 `list it in consumed_shared_args instead` 直接把下一步动作写给程序员。

**练习 3**：`--line-a-flag` 与 `-q` 都触发拼写账 panic，`LINE_SHARED_ALIAS`（长别名）与 `LINE_SHORT_ALIAS`（短别名）两个用例分别证明了什么？
**答案**：`LINE_SHARED_ALIAS` 用 `.alias("tp-size")` 证明**长别名与主长拼写同账**（[../pegainfer-frontend/src/model_line.rs:802-813](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L802-L813)、[L946-952](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L946-L952)）；`LINE_SHORT_ALIAS` 用 `.short_alias('q')` 证明**短拼写空间同样入账**，且 short 与 short_alias 共享一个键空间（[L814-830](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L814-L830)、[L954-960](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L954-L960)）。短拼写只有一个字符，撞车概率远高于长拼写，所以必须显式管。

### 4.2 构造期自检流水线：ModelLineRegistry::new 与 build_command

#### 4.2.1 概念说明

`ModelLineRegistry::new(lines)` 在进程启动时跑一遍完整自检，产出两样东西：

- `entries`：每条线的 `LineEntry`（线引用 + 该线独占 flag 的 id 集合 `own_ids`）；
- `requirements`：依赖表（source id → targets），供 `build_command` 挂到 clap 的 `requires_all` 上（u2-l2 讲过这层「依赖门」）。

自检的顺序有讲究：**`SharedArgs` 先入账，各线后入账**。这样任何线想抢占共享 flag 的 id/拼写都会立刻撞账，且 `consumed_shared_args` 的核对（「你声称消费的共享 id 必须真的存在」）有账可查。

还有一组容易混淆的「两份命令」要分清：

- **probe command**：`register_line` 里 `line.augment_cli(clap::Command::new("probe"))` 造出的、**只含该线独占 flag** 的临时命令，仅用于把独占 flag 登记进账本、收集 `own_ids`；
- **合并命令**：`build_command` 用「`SharedArgs::augment_args` + 每条线 `augment_cli`」铸出的真实服务命令（`--help` 展示的就是它）。

两者走同一套 `augment` 逻辑，所以账本对真实命令成立。

#### 4.2.2 核心流程

`new` 的流水线（按源码顺序）：

```text
1. SharedArgs::augment_args(空命令) → 全部共享 flag 以 owner=Shared 入账
2. 对 CORE_ARGS 的每个 id:  assert 它确实是已登记的共享 id
3. pending_requirements ← SHARED_ARG_REQUIREMENTS（共享层的依赖表）
4. 对每条线:
     a. assert 线名唯一（line_names 集合去重）
     b. register_line:
          - 对 consumed_shared_args 的每个 id:
              assert 它是已登记的共享 id（防拼写错/防私造）
          - augment_cli 出 probe command → 独占 flag 全部入账（owner=Line(线名)）
          - 返回 own_ids
     c. pending_requirements ← 该线 arg_requirements
5. 对 pending_requirements 的每条:
     - validate_requirement（4.1 的三断言）
     - assert 同一 source 没被声明两次
6. 产出 ModelLineRegistry { entries, requirements }
```

注意第 5 步**收齐了才统一校验**：依赖表的 target 允许指向任何共享 id 或同线 id，先把所有线登记完再验，才能让「target 存在性」有完整账本可查。

#### 4.2.3 源码精读

`ModelLineRegistry` 的结构：

> [../pegainfer-frontend/src/model_line.rs:497-501](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L497-L501)
> 两个字段：`entries`（每条线的引用 + own_ids）与 `requirements`（依赖表）。`LineEntry` 定义在 [L385-388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L385-L388)。

`new` 的完整流水线，开头的文档注释一语道破存在理由：

> [../pegainfer-frontend/src/model_line.rs:503-548](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L503-L548)
> L504-505：「Validates CLI ownership explicitly because Clap's equivalent schema assertions are disabled in release builds」——release 构建里 clap 不查，注册表自己查。随后按 4.2.2 的六步执行；`CORE_ARGS`（[L68-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L68-L74)）的四个 id 在 L510-515 被逐一核对，`SHARED_ARG_REQUIREMENTS`（[L223-232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L223-L232)）在 L516-519 成为 pending_requirements 的起点。

`register_line` —— 单线登记：

> [../pegainfer-frontend/src/model_line.rs:480-495](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L480-L495)
> 先核对 `consumed_shared_args` 的每个 id 都是真实共享 id（`LINE_UNKNOWN_SHARED` 用例锁住这条：期望 panic 子串 `consumes unknown SharedArgs flag id "not_a_shared_arg"`，[L962-966](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L962-L966)），再用 probe command 收集 `own_ids`。

`build_command` —— 真实命令的铸造口：

> [../pegainfer-frontend/src/model_line.rs:550-564](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L550-L564)
> 先 `SharedArgs::augment_args`，再逐线 `augment_cli`，最后 `mut_args` 把依赖表挂到对应 arg 的 `requires_all` 上。注释解释了为什么不用 `mut_arg`：它会重新 append 参数、打乱 `--help` 的顺序。

server 侧的构造点（承接 u1-l3 的流程图）：

> [src/main.rs:151-160](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L151-L160)
> `ModelLineRegistry::new(model_lines())` 在 main 的第一步执行——任何注册表缺陷都在解析 CLI 之前就 panic，此时连 config.json 都还没读。

#### 4.2.4 代码实践

1. **实践目标**：验证 `CORE_ARGS` 的四个核心 flag 确实全部存在于 `SharedArgs`，并预测「往 `CORE_ARGS` 里写错一个 id」的后果。
2. **操作步骤**：
   - 对照 [../pegainfer-frontend/src/model_line.rs:68-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L68-L74)（`model_path` / `served_model_name` / `port` / `tool_call_parser`）与 `SharedArgs` 的字段定义（[L100-221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L100-L221)），确认一一对应。
   - 在自己工作副本里把 `CORE_ARGS` 加一项 `"typo_id"`，运行 `cargo run --release -- --version`，观察 panic。
3. **需要观察的现象**：进程在 main 早期 panic，消息为 `CORE_ARGS contains unknown SharedArgs flag id "typo_id"`（格式串见 [../pegainfer-frontend/src/model_line.rs:510-515](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L510-L515)）。
4. **预期结果**：核对通过；panic 复现成功（改完记得还原，勿提交）。
5. 运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`new` 为什么必须「先共享、后各线」入账，倒过来会怎样？
**答案**：`claim_id` 的指向性分支（「list it in consumed_shared_args instead」）依赖账本里**已有** Shared 的记录才能触发；`consumed_shared_args` 的核对也要求共享 id 先登记。若倒过来，线先入账，`tp_size` 会被记成线所有，随后共享 flag 入账时走通用 panic 分支，消息丢失了「该列进 consumed_shared_args」的修复指引。

**练习 2**：`requirements` 为什么攒到所有线登记完才统一校验，而不是逐线即校验？
**答案**：`validate_requirement` 的 target 允许指向共享 id 或**同线** id；共享 id 虽已先入账，但逐线校验无法区分「target 是别线的合法 id」与「target 不存在」两种情况之外的成本问题——收齐后统一校验（[../pegainfer-frontend/src/model_line.rs:536-543](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L536-L543)）让所有线的 id 都在账上，一次 assert 全部查清，同时顺带断言了同一 source 不会被声明两次。

**练习 3**：probe command（`Command::new("probe")`）与 `build_command` 产出的命令差在哪？为什么自检用前者就够了？
**答案**：前者只含**单条线**的独占 flag（外加 `augment_cli` 自己加的东西），后者含共享 + 全部线的 flag。自检只关心「谁的 flag 撞谁的账」，而账本按 owner 分账登记——probe command 足以把该线独占 flag 全部入账（owner=该线）；共享 flag 已在第一步以 owner=Shared 入账。合并命令里 flag 的存在性由两本账的并集保证。

### 4.3 detect：配置归属的裁决

#### 4.3.1 概念说明

`detect(config)` 回答「这份 config.json 归哪条编译进来的线」，三种结果：

| 结果 | 含义 | 谁的问题 |
|------|------|----------|
| `Ok(line)` | 恰好一条线的 `probe` 认领 | —— |
| `Err(Conflict)` | 两条及以上线认领同一份配置 | **注册表 bug**（两条线身份重叠），用户无法修复 |
| `Err(NoMatch)` | 没有任何线认领 | 用户可行动：目录配错，或模型家族没编译进来（→ u1-l4 的 feature 提示） |

契约（来自 trait 文档）：每条线的 `probe` 只做**身份**检查（`model_type` / `architectures`），尺寸等校验留给后续阶段；注册表的要求是「恰好一条线认领」。

#### 4.3.2 核心流程

```text
rejections = []
claimed = None
对每条线:
    probe(config):
        Ok  → 若 claimed 为空则记下; 否则立即 return Err(Conflict{first: 前者, second: 本线})
        Err → rejections.push("<线名>: <拒绝原因>")
若 claimed 为 None:
    return Err(NoMatch{ model_type: config 里的值或 "missing",
                        architectures: 同上,
                        rejections })
return Ok(claimed)
```

要点：`Conflict` **立即返回**（第一条冲突即可定性为注册表 bug，无须枚举）；`NoMatch` 则把每条线的拒绝原因全部拼进错误，让读者一眼看到「谁拒了、为什么拒」。

#### 4.3.3 源码精读

`detect` 主体：

> [../pegainfer-frontend/src/model_line.rs:566-601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L566-L601)
> 收集循环在 L575-588：第二个 `Ok(())` 触发 `Conflict` 并点名 `prev.name()` 与 `entry.line.name()`；每个 `Err(reason)` 变成一行 `"<线名>: {reason}"`。L589-600 的闭包 `render` 把 `model_type` / `architectures` 渲染为原文，缺键时输出 `missing`。

两种错误的 `thiserror` 模板——模板即 API：

> [../pegainfer-frontend/src/model_line.rs:27-46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L27-L46)
> `Conflict` 渲染为 `model config claimed by both {first} and {second}`；`NoMatch` 渲染为 `no compiled-in model line claims this config (model_type=..., architectures=...); rejections: <分号连接的逐线原因>`。测试逐字断言这些渲染（[L851-890](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L851-L890)）。

server 的接线（承接 u1-l4）：

> [src/main.rs:168-175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168-L175)
> server 只对 `NoMatch` 分支附加 `feature_gate_hint`；`Conflict` 原样上抛——因为它指向注册表缺陷，提示用户重建 feature 毫无意义。

测试参照：

> [../pegainfer-frontend/src/model_line.rs:851-865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L851-L865)
> `detect_conflict_names_both_lines` 用 `LINE_B` + `LINE_B_TWIN`（两条都认领 `model_b`，[L759-770](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L759-L770)）断言消息同时包含 `LineB` 与 `LineBTwin`。
>
> [../pegainfer-frontend/src/model_line.rs:867-890](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L867-L890)
> `detect_no_match_renders_identity_fields_verbatim` 断言 `model_type=123`（非字符串也原样渲染）与 `architectures="Foo"`；`detect_no_match_reports_missing_fields` 断言空 config 渲染为 `model_type=missing`。

#### 4.3.4 代码实践

1. **实践目标**：在无 GPU 的草稿环境里亲手触发 `detect` 的三种结果，核对错误消息原文。
2. **操作步骤**：按第 5 节综合实践搭好 `registry-lab` 草稿 crate 后运行：
   ```bash
   cargo test --test registry_lab detect
   ```
   （综合实践中的 `detect_three_outcomes` 测试覆盖三种 config：`{"model_type":"model_b"}` 命中、`{"model_type":"nope"}` 全拒、双胞胎线认领同一 `model_type`。）
3. **需要观察的现象**：命中路径返回 `LineB`；全拒路径消息以 `no compiled-in model line claims this config (model_type=nope, architectures=missing); rejections: LineA: ...; LineB: ...` 开头；双认领路径消息为 `model config claimed by both LineB and LineBTwin`。
4. **预期结果**：三条断言全绿。
5. 运行结果**待本地验证**；消息原文引自 [../pegainfer-frontend/src/model_line.rs:29-45](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L29-L45) 的模板与 [L851-865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L851-L865) 的测试断言。

#### 4.3.5 小练习与答案

**练习 1**：`Conflict` 为什么收集到第一条冲突就立即返回，而 `NoMatch` 要枚举全部拒绝原因？
**答案**：`Conflict` 说明注册表里两条线身份重叠——这是代码缺陷，用户改命令行无法解决，第一条冲突已足以定性；`NoMatch` 是用户可行动的错误，逐线列出拒绝原因（每条 probe 的 `Err` 字符串）才能帮用户/开发者判断是目录配错还是家族没编译进来。

**练习 2**：config 是 `{}`（空对象）时，`NoMatch` 的 `model_type` 字段渲染成什么？
**答案**：`missing`。`render` 闭包在键缺失时返回 `"missing"`（[../pegainfer-frontend/src/model_line.rs:589-594](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L589-L594)），对应测试 `detect_no_match_reports_missing_fields`。

**练习 3**：`probe` 只做身份检查、不做尺寸校验，这个契约放错了顺序会发生什么？
**答案**：若 `probe` 里做尺寸校验（如层数、头数），同一 `model_type` 的量化变体或裁剪版就会被 `probe` 拒绝，`detect` 会把「家族对了但尺寸不匹配」报告成 `NoMatch`——而 u1-l4 讲过 `feature_gate_hint` 只看身份，这种配置会得到「rebuild with --features」的错误指引，实际上是南辕北辙。尺寸校验放在 detect 之后的 `launch` 前后（深层 anyhow 链）才对。

### 4.4 validate_provided：flag 归属的裁决（consume-or-reject）

#### 4.4.1 概念说明

`detect` 裁决**配置**归属，`validate_provided` 裁决**flag**归属。它执行 u2-l2 提出的 consume-or-reject：

> 每个显式提供的 flag 必须是（1）核心 flag、（2）被检测线消费的共享 flag、（3）该线独占 flag——三者之一，否则拒绝。

形式化一点：设 \(\ell\) 为检测到的线、\(P\) 为 `provided_args` 算出的显式集合、\(\mathrm{CORE}\) 为核心 id 集、\(\mathrm{consumed}(\ell)\) 为该线声明的共享子集、\(\mathrm{own}(\ell)\) 为其独占 id 集，则校验断言

\[
\forall x \in P:\quad x \in \mathrm{CORE} \;\lor\; x \in \mathrm{consumed}(\ell) \;\lor\; x \in \mathrm{own}(\ell),
\]

违例即返回 \( \mathrm{UnconsumedFlag}(x, \ell) \)。

为什么必须这样执法？因为合并命令里**所有线的 flag 都在**（`build_command` 的产物），clap 解析 `--line-b-flag` 时不会知道「当前检测到的是 LineA」。归属裁决只能发生在「检测完成之后」，这正是 u1-l3 流程图中 `validate_provided` 位于 `detect` 之后的原因。

#### 4.4.2 核心流程

```text
在 entries 里用指针相等找到 detected 对应的 entry   # 注册表只信自己发出的引用
对 provided 里的每个 id:
    若 id ∈ CORE_ARGS                     → 放行
    若 id ∈ detected.consumed_shared_args → 放行
    若 id ∈ entry.own_ids                 → 放行
    否则 → return Err(CliError::UnconsumedFlag { flag: long_flag(cmd, id), line: 线名 })
```

`long_flag` 把程序内 id 翻译回用户看得懂的长拼写：在 `cmd` 里找同名 arg，取其 `get_long()`；找不到就退回 id 本身。

#### 4.4.3 源码精读

`validate_provided` 主体：

> [../pegainfer-frontend/src/model_line.rs:603-630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L603-L630)
> L611-615 用 `std::ptr::eq(entry.line, detected)` 定位条目并 `expect("detected line came from this registry")`——只接受本注册表发出的线引用；L616-628 是三条件放行 + 违例返回，顺序与 4.4.2 一致。

`long_flag` —— id 到拼写的回译：

> [../pegainfer-frontend/src/model_line.rs:633-638](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L633-L638)
> 注意它返回**不带** `--` 前缀的裸长名，前缀由错误模板补上——与 `claim_spelling` 的账本键（自带前缀）约定不同，读代码时别混淆。

`CliError::UnconsumedFlag` 的模板：

> [../pegainfer-frontend/src/model_line.rs:50-60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L50-L60)
> `"--{flag} is not used by {line}"`——这个字符串是被测试逐字锁住的 API，改一个字都会破坏下游（模型 crate 的单测）。

两个测试把「B 线 flag」与「未消费共享 flag」两种违例都锁住了：

> [../pegainfer-frontend/src/model_line.rs:905-926](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L905-L926)
> `validate_provided_rejects_another_lines_flag_by_long_name` 断言错误原文恰为 `--line-b-flag is not used by LineA`；`validate_provided_rejects_unconsumed_shared_flag` 断言 `--kv-offload is not used by LineA`（`kv_offload` 是共享 flag，而 `LINE_A` 的 `consumed` 只有 `tp_size`）。放行路径的三条件由 [L892-903](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L892-L903) 的 `validate_provided_allows_core_shared_and_own_flags` 覆盖（`port` 核心 + `tp_size` 被消费 + `line_a_flag` 独占，三者同时放行）。

server 侧调用点：

> [src/main.rs:177-178](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L177-L178)
> `registry.validate_provided(line, &provided, &cmd)?` 紧跟在 `detect` 之后、`shared.validate` 与 `line.validate` 之前——先定归属，再查语义（u2-l2 的四道门里的「归属门」）。

#### 4.4.4 代码实践

1. **实践目标**：亲手触发两种 `UnconsumedFlag`，核对错误消息原文，并确认它们来自归属门而非 clap。
2. **操作步骤**：搭好第 5 节的 `registry-lab` 后运行：
   ```bash
   cargo test --test registry_lab unconsumed
   ```
   `another_lines_flag_is_rejected_for_line_a` 用**两条线**的注册表：`build_command` 出的合并命令里 `--line-b-flag` 存在，clap 解析通过，随后 `validate_provided(&LINE_A, ...)` 拒绝它。
3. **需要观察的现象**：`error.to_string()` 逐字等于 `--line-b-flag is not used by LineA`（B 线独占 flag）；对单线注册表用 `parse_for_line(&LINE_A, &["pegainfer", "--kv-offload"])` 时得到 `--kv-offload is not used by LineA`（共享 flag 未被消费）。
4. **预期结果**：两条断言通过；两种违例的消息结构一致，仅 flag 与线名不同。
5. 运行结果**待本地验证**；消息原文引自 [../pegainfer-frontend/src/model_line.rs:54-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L54-L55) 与 [L905-926](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L905-L926)。

#### 4.4.5 小练习与答案

**练习 1**：`validate_provided` 为什么拿到的入参是 `provided` 集合，而不是整个 `SharedArgs` 结构？
**答案**：`SharedArgs` 里满是默认值（`port=8000`、`tp_size=1`……），它们不是用户敲的。若按「结构里有什么就查什么」执法，每条线都会被自己的默认值误伤。`provided_args`（[../pegainfer-frontend/src/model_line.rs:640-661](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L640-L661)）只保留 `ValueSource` 为命令行/环境变量的 id——值相同出身可以不同，所以只能按出身过滤（u2-l2 的结论）。

**练习 2**：错误消息里的 `--line-b-flag` 前缀是谁加的？`long_flag` 找不到对应 arg 时会输出什么？
**答案**：`--` 前缀由 `thiserror` 模板 `"--{flag} is not used by {line}"` 加，`long_flag` 只返回裸长名 `line-b-flag`；找不到 arg 时退回 id 本身（[../pegainfer-frontend/src/model_line.rs:633-638](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L633-L638)）。实践中找不到的情形几乎不出现，因为 `provided` 的 id 本来就来自同一份 `cmd` 的解析。

**练习 3**：为什么定位条目用 `std::ptr::eq` 而不是比较线名？
**答案**：`detect` 返回的就是注册表持有的同一个 `&'static dyn ModelLine` 引用，指针相等是最直接也最严格的同一性判据（trait 对象无法 `==` 比较）。`expect("detected line came from this registry")` 同时表达了契约：传进来的线必须出自本注册表——拿别处构造的线引用来查账是调用方 bug。

### 4.5 parse_for_line：把装配链包成测试帮手

#### 4.5.1 概念说明

`parse_for_line(line, argv)` 是一个 `pub` 的**测试帮手**：为单条线建一个临时注册表，然后按 server 的真实顺序走完「建命令 → 解析 → 取 SharedArgs → 算 provided → 归属校验 → 共享语义校验」六步，返回三元组 `(SharedArgs, ArgMatches, provided)`。模型 crate 的单元测试用它验证「我的线的 CLI 规则」，不必拉起整个 server。

它的文档注释写明定位：「Exposed for model-crate unit tests; not a serving entry point」。

**一个必须想清楚的边界**：`parse_for_line` 建的是**单线**注册表——合并命令里**只有**共享 flag + 这一条线的独占 flag。因此：

- 传一个「别的线才有的 flag」不会得到 `UnconsumedFlag`，而是在 clap 解析那一步就报 **unexpected argument**（那个拼写根本不在命令里）；
- `UnconsumedFlag` 只在「flag 在命令中存在（clap 解析通过），但对这条线无归属」时出现。单线场景下能触发它的典型输入是：**共享 flag 但该线不消费**（如对 `consumed = ["tp_size"]` 的线传 `--kv-offload`）。

想复现「B 线独占 flag 对 A 线报 `UnconsumedFlag`」的完整场景，就得像源码测试那样用**两条线**的注册表手工调 `validate_provided`（见 4.4.4 实践）。

#### 4.5.2 核心流程

```text
parse_for_line(line, argv):
    registry = ModelLineRegistry::new([line])          # 单线注册表（同样过全部自检）
    cmd      = registry.build_command(Command::new("pegainfer"))
    matches  = cmd.try_get_matches_from(argv)?          # clap 值门 + 依赖门（u2-l2）
    shared   = SharedArgs::from_arg_matches(matches)?
    provided = provided_args(matches, cmd)
    registry.validate_provided(line, provided, cmd)?    # 归属门 → UnconsumedFlag
    shared.validate(provided)?                          # 共享语义门 → CliError::Rule
    return (shared, matches, provided)
```

与 [src/main.rs:151-178](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L151-L178) 逐行对照：前五步与 main 的对应步骤同构（main 还多了读 config.json、`detect`、`line.validate`、`serve_plan` 这些需要模型目录的步骤，帮手刻意不碰）。错误统一收进 `anyhow::Result`，调用方 `.unwrap_err().to_string()` 就能拿到首行原文。

#### 4.5.3 源码精读

`parse_for_line` 全文：

> [../pegainfer-frontend/src/model_line.rs:665-681](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L665-L681)
> L673 单线注册表；L674-675 建命令并克隆一份供 `provided_args` 查 real id 集合；L678-679 依次过归属门与共享语义门。注意它**不调用** `line.validate`——那需要 `LaunchContext`（config.json 与模型路径），超出 CLI 帮手职责。

`provided_args` 的实现（u2-l2 已详解，此处给链接供对照）：

> [../pegainfer-frontend/src/model_line.rs:640-661](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L640-L661)
> 过滤掉 clap 的合成 group id（L643-646 先用 `cmd.get_arguments()` 收集真实 id 集合），再按 `ValueSource` 保留命令行/环境变量来源。

它是被谁用的：模型 crate 的单元测试按 `cli(ctx)` 模式取回自己的 CLI（u2-l1），而测试里驱动整条链的入口就是这个帮手——server 二进制自身用的是 `registry.build_command + provided_args + validate_provided` 的展开形式（[src/main.rs:151-160](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L151-L160)、[L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L177)），两条路径共享同一套实现。

#### 4.5.4 代码实践

1. **实践目标**：用 `parse_for_line` 分别触发「未消费共享 flag」与「命令里根本没有的 flag」，对比两种错误的来源。
2. **操作步骤**（在第 5 节 `registry-lab` 的测试里完成）：
   ```rust
   // 场景 A：共享 flag 存在于命令中，但 LINE_A 不消费 → UnconsumedFlag
   let err = parse_for_line(&LINE_A, &["pegainfer", "--kv-offload"]).unwrap_err();
   assert_eq!(err.to_string(), "--kv-offload is not used by LineA");

   // 场景 B：单线命令里根本没有这个拼写 → clap 的 unexpected argument
   let err = parse_for_line(&LINE_A, &["pegainfer", "--line-b-flag"]).unwrap_err();
   assert!(err.to_string().contains("unexpected argument"));
   ```
3. **需要观察的现象**：场景 A 报 `--kv-offload is not used by LineA`（归属门，`CliError`）；场景 B 报 clap 的解析错误（值门都还没过，因为 `--line-b-flag` 不在单线命令里）。
4. **预期结果**：两条断言通过；能说清「`UnconsumedFlag` 需要 flag 存在于合并命令中」这一前提。
5. 运行结果**待本地验证**；场景 A 的原文引自 [../pegainfer-frontend/src/model_line.rs:917-926](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L917-L926) 的等价断言，场景 B 的措辞来自 clap 4 的标准错误（`unexpected argument`），具体呈现**待本地确认**。

#### 4.5.5 小练习与答案

**练习 1**：`parse_for_line` 为什么不调用 `line.validate`？
**答案**：`line.validate` 的入参是 `LaunchContext`，其中含 `config.json` 与模型路径（[../pegainfer-frontend/src/model_line.rs:273-284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L273-L284)）——线内冲突矩阵（u2-l4 的主题）常要读配置或合并命令的完整 matches。帮手的定位是「无模型目录也能测 CLI 骨架」，所以止步于共享语义门；`line.validate` 由 server 主流程在 detect 之后调用（[src/main.rs:185](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L185)）。

**练习 2**：`parse_for_line` 的 `argv` 第一个元素为什么要写 `"pegainfer"` 这种占位程序名？
**答案**：`try_get_matches_from` 按 clap 约定把迭代器的**第一个元素当作程序名**（argv[0]），后续元素才是真正的参数。源码测试同样带占位名（[../pegainfer-frontend/src/model_line.rs:978-981](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L978-L981) 用 `"test"`）。漏写会把第一个 flag 当成程序名，解析结果错位。

**练习 3**：帮手内部 `cmd.clone().try_get_matches_from(argv)` 为什么必须 clone 一份命令？
**答案**：`try_get_matches_from` 会消耗（take ownership）`Command`，而后面 `provided_args(&matches, &cmd)` 还要用**同一份命令定义**查真实 arg id 集合、`validate_provided` 里的 `long_flag` 也要在它上面回译拼写。先 clone 再解析，保留一份未被消耗的定义。

## 5. 综合实践：registry-lab 草稿 crate

把本讲全部内容串成一个可在无 GPU 机器上运行的实验场。目标：亲手看到构造期 panic、detect 三态、UnconsumedFlag 原文，全程不改 PegaInfer 源码。

### 5.1 建 crate

在 **openinfer checkout 之外**的目录（避免被 workspace 吸收）：

```bash
cargo new --lib registry-lab
cd registry-lab
```

编辑 `Cargo.toml`（`path` 按你的 checkout 位置调整；示例代码）：

```toml
[dev-dependencies]
pegainfer-frontend = { path = "/path/to/openinfer/pegainfer-frontend" }
clap = "4"
serde_json = "1"
anyhow = "1"
```

> `pegainfer-frontend` 是纯 Rust crate（无 CUDA 依赖，见其 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/Cargo.toml#L7-L34)），首次编译需拉起 axum/tokio/vllm-server 等依赖树，约几分钟。

### 5.2 写测试

创建 `tests/registry_lab.rs`（示例代码，仿 [../pegainfer-frontend/src/model_line.rs:687-746](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L687-L746) 的 `StubLine` 模式）：

```rust
use clap::Command;
use pegainfer_frontend::engine::LaunchedEngine;
use pegainfer_frontend::model_line::{
    DetectError, LaunchContext, ModelLine, ModelLineRegistry, parse_for_line, provided_args,
};

struct StubLine {
    name: &'static str,
    claims: &'static str,
    augment: Option<fn(Command) -> Command>,
    consumed: &'static [&'static str],
}

impl StubLine {
    const fn new(
        name: &'static str,
        claims: &'static str,
        augment: Option<fn(Command) -> Command>,
    ) -> Self {
        Self { name, claims, augment, consumed: &["tp_size"] }
    }
}

impl ModelLine for StubLine {
    fn name(&self) -> &'static str {
        self.name
    }
    fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
        let model_type = config.get("model_type").and_then(|v| v.as_str());
        if model_type == Some(self.claims) {
            Ok(())
        } else {
            Err(format!("model_type {model_type:?} is not {:?}", self.claims))
        }
    }
    fn augment_cli(&self, cmd: Command) -> Command {
        match self.augment {
            Some(augment) => augment(cmd),
            None => cmd,
        }
    }
    fn consumed_shared_args(&self) -> &'static [&'static str] {
        self.consumed
    }
    fn launch(&self, _ctx: &LaunchContext<'_>) -> anyhow::Result<LaunchedEngine> {
        unreachable!("registry lab never launches")
    }
}

static LINE_A: StubLine = StubLine::new("LineA", "model_a", Some(|cmd| {
    cmd.arg(clap::Arg::new("line_a_flag").long("line-a-flag").num_args(0))
}));
static LINE_B: StubLine = StubLine::new("LineB", "model_b", Some(|cmd| {
    cmd.arg(clap::Arg::new("line_b_flag").long("line-b-flag").num_args(0))
}));
static LINE_B_TWIN: StubLine = StubLine::new("LineBTwin", "model_b", None);
// 同拼写、不同 id：只撞拼写账，不撞 id 账。
static LINE_SPELL_CLASH: StubLine = StubLine::new("LineSpellClash", "model_x", Some(|cmd| {
    cmd.arg(clap::Arg::new("another_id").long("line-a-flag").num_args(0))
}));

// —— 4.1：构造期拼写冲突 panic ——
#[test]
#[should_panic(expected = "both define CLI spelling \"--line-a-flag\"")]
fn spelling_clash_panics_at_construction() {
    let _ = ModelLineRegistry::new(vec![&LINE_A, &LINE_SPELL_CLASH]);
}

// —— 4.3：detect 三态 ——
#[test]
fn detect_three_outcomes() {
    let registry = ModelLineRegistry::new(vec![&LINE_A, &LINE_B]);
    let hit = registry
        .detect(&serde_json::json!({"model_type": "model_b"}))
        .expect("unique claimant");
    assert_eq!(hit.name(), "LineB");

    let nope = registry
        .detect(&serde_json::json!({"model_type": "nope"}))
        .expect_err("nothing claims this");
    assert!(matches!(nope, DetectError::NoMatch { .. }));
    let msg = nope.to_string();
    assert!(msg.starts_with("no compiled-in model line claims this config"), "{msg}");
    assert!(msg.contains("rejections:"), "{msg}");

    let twins = ModelLineRegistry::new(vec![&LINE_B, &LINE_B_TWIN]);
    let conflict = twins
        .detect(&serde_json::json!({"model_type": "model_b"}))
        .map(ModelLine::name)
        .expect_err("two claimants");
    assert!(matches!(conflict, DetectError::Conflict { .. }));
    assert_eq!(
        conflict.to_string(),
        "model config claimed by both LineB and LineBTwin"
    );
}

// —— 4.4：consume-or-reject（两条线的合并命令） ——
#[test]
fn another_lines_flag_is_rejected_for_line_a() {
    let registry = ModelLineRegistry::new(vec![&LINE_A, &LINE_B]);
    let cmd = registry.build_command(Command::new("pegainfer"));
    let matches = cmd
        .clone()
        .try_get_matches_from(["pegainfer", "--line-b-flag"])
        .expect("flag exists in the merged command");
    let provided = provided_args(&matches, &cmd);
    let error = registry
        .validate_provided(&LINE_A, &provided, &cmd)
        .expect_err("LineB's flag must be rejected for LineA");
    assert_eq!(error.to_string(), "--line-b-flag is not used by LineA");
}

// —— 4.5：parse_for_line 的单线边界 ——
#[test]
fn parse_for_line_unconsumed_shared_flag() {
    let error = parse_for_line(&LINE_A, &["pegainfer", "--kv-offload"])
        .expect_err("kv_offload is not in LINE_A's consumed list");
    assert_eq!(error.to_string(), "--kv-offload is not used by LineA");

    let unknown = parse_for_line(&LINE_A, &["pegainfer", "--line-b-flag"])
        .expect_err("not in the single-line command at all");
    let msg = unknown.to_string();
    assert!(msg.contains("unexpected argument"), "{msg}");
}
```

### 5.3 运行与核对清单

```bash
cargo test
```

逐项核对（消息原文的源码依据列在右侧）：

| 现象 | 依据 |
|------|------|
| `spelling_clash_panics_at_construction` 通过 | [../pegainfer-frontend/src/model_line.rs:934-938](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L934-L938) 的同构用例 |
| `detect` 冲突消息逐字相等 | 模板 [L29-33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L29-L33) + 断言 [L851-865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L851-L865) |
| `--line-b-flag is not used by LineA` | 模板 [L54-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L54-L55) + 断言 [L905-915](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L905-L915) |
| `--kv-offload is not used by LineA` | 断言 [L917-926](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L917-L926) |
| `unexpected argument` | clap 4 标准错误（措辞**待本地确认**） |

本讲义写作环境未获准执行 `cargo test`，上述结果**待本地验证**。若某条断言失败，优先怀疑自己的 `StubLine` 与源码版本不一致（例如 `consumed` 列表不同会改变 `--kv-offload` 的归属结论）。

### 5.4 收尾

实验完毕可整目录删除；PegaInfer 源码自始至终未被修改。

## 6. 本讲小结

- `RegistryClaims` 用**两本账**（id 账 + 拼写账，值均为 `ArgOwner`）在注册表构造期查出所有 flag 冲突，撞车即 `panic!`——因为 clap 的等价 schema 断言在 release 构建中被禁用，而服务端二进制永远以 `--release` 发布。
- `ModelLineRegistry::new` 的自检顺序是「共享先入账 → `CORE_ARGS` 核对 → 逐线登记（线名唯一、`consumed_shared_args` 必须指向真实共享 id、独占 flag 入账）→ 依赖表收齐统一校验」；`build_command` 另铸真实合并命令并把依赖表挂到 `requires_all`。
- `detect` 的契约是**恰好一条线认领**：两条线认领即 `Conflict`（注册表 bug，点名双方、立即返回）；无人认领则 `NoMatch`（逐线拼 `rejections`，server 仅在此分支附加 feature 重建提示）。
- `validate_provided` 是 consume-or-reject 的执法点：核心 flag / 被消费共享 flag / 线独占 flag 三者之一放行，否则 `CliError::UnconsumedFlag`，消息原文（`--{flag} is not used by {line}`）是被测试逐字锁住的 API。
- `parse_for_line` 把整条「解析 + 校验」链包成模型 crate 可用的测试帮手，但建的是**单线**注册表——别的线的独占 flag 在它那里报 clap 的 unexpected argument，而不是 `UnconsumedFlag`。

## 7. 下一步学习建议

- **u2-l4（一条真实模型线：Qwen3Line 的独占 CLI 与校验矩阵）**：本讲的 `StubLine` 是三方法最小实现，下一讲看 8 方法满配的真实线——`arg_requirements` 如何声明依赖、`validate` 的冲突矩阵怎么写、`launch` 如何折叠选项，全都建立在本讲的注册表语义之上。
- **u2-l5（ServePlan）**：看注册表之外「引擎加载前前端就要知道的事实」如何流出模型线。
- 建议再读一遍 [../pegainfer-frontend/src/model_line.rs:683-987](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L683-L987) 的测试模块——本讲引用的每个行为在那里都有一条逐字断言，是最好的「可执行文档」；学完 u2-l4 后回头重读，你会看出 `LINE_DANGLING_REQUIREMENT` 这类 stub 正是各模型线真实犯错模式的浓缩。
