# 环境变量与配置层级

## 1. 本讲目标

上一讲（u6-l1）我们看清了配置文件是如何被解析、如何按 system→global→local→worktree→command 的顺序加载进 `config_set` 的。本讲要回答一个更实际的问题：**我不想改配置文件，只想「这一次」临时换一个仓库目录、换一个用户名、注入一个秘密值，该怎么办？**

答案就是 **环境变量** 和 **`-c` / `--config-env` 命令行选项**。学完本讲你应当能够：

1. 说清 git 里「环境变量」分哪两类、各自在什么时机被读取。
2. 画出一条配置值从「最低优先级」到「最高优先级」的完整优先级链。
3. 区分 `GIT_DIR` 这类「直接覆盖仓库上下文」的环境变量，与 `GIT_CONFIG_PARAMETERS` 这类「伪装成配置项」的环境变量。
4. 用 `git -c` 与 `git --config-env` 在不落盘的前提下覆盖任意配置，并理解它们为何比环境变量更安全。

## 2. 前置知识

- **环境变量（environment variable）**：操作系统给每个进程的一张「名=值」字符串表。子进程默认继承父进程的整张表。C 语言里用 `getenv("名字")` 读、`setenv("名字","值",1)` 写。
- **进程的命令行参数 `argv`**：程序启动时收到的字符串数组，`argv[0]` 是程序名，`argv[1]` 起是参数。命令行参数对同机其他用户可见（如 `ps`），而环境变量默认只对本进程及其子进程可见——这点对放「秘密值」很关键。
- **优先级（precedence）**：git 同一个配置键可能出现在多个来源里。本讲里「最后读到的获胜」是铁律，所以「读取顺序」就等于「优先级从低到高」。
- **承接 u6-l1**：你已经知道配置是「回调驱动」逐条解析的，`do_git_config_sequence` 决定读取顺序，每条值都带一个 `struct key_value_info`（出身信息）。本讲只补充「环境变量」这一路来源，不重复解析器细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [environment.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h) | 用 `#define` 集中登记所有 `GIT_*` 环境变量名，并声明 `local_repo_env[]` 数组与 `struct repo_config_values`。 |
| [environment.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c) | 定义 `local_repo_env[]`、`getenv_safe()`，以及若干「读环境变量并落成全局配置」的小函数。 |
| [git.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c) | 总分发器里的 `handle_options()`：在子命令运行前，把 `-c`/`--git-dir`/`-C` 等全局选项翻译成环境变量。 |
| [config.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c) | 配置加载主战场：`do_git_config_sequence()` 给出读取顺序，`git_config_push_parameter()` 把 `-c` 转成环境变量，`git_config_from_parameters()` 再把环境变量读回成配置。 |
| [config.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h) | 定义 `enum config_scope`（配置作用域）与 `enum config_origin_type`（配置来源类型），每条配置值的出身都靠它们标记。 |
| [parse.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse.c) | 提供 `git_env_bool()`：把环境变量当布尔值读的通用助手。 |

## 4. 核心概念与源码讲解

git 的环境变量看似很多，但按「读取时机与用途」可以干净地分成两类。本讲按三个最小模块推进：先认识环境变量本身（4.1），再看它们如何接入配置优先级链（4.2），最后看 `-c`/`--config-env` 这条「把命令行变成配置」的捷径（4.3）。

### 4.1 环境变量定义与读取

#### 4.1.1 概念说明

git 不把环境变量名当成裸字符串散落在代码各处，而是统一用 `#define` 给每个名字起一个 `_ENVIRONMENT` 后缀的宏，集中在 `environment.h`。这样做的好处是：改名只改一处，且编译器能帮你查拼写。

更关键的是，git 的环境变量天然分成两个家族，**它们的生效路径完全不同**：

1. **上下文型环境变量**（`GIT_DIR`、`GIT_WORK_TREE`、`GIT_OBJECT_DIRECTORY`、`GIT_INDEX_FILE`、`GIT_CEILING_DIRECTORIES` …）。
   它们**不是配置项**，而是直接告诉 git「你的仓库在哪、工作树在哪、对象库在哪」。它们在「仓库发现」阶段（`setup.c`，见 u2-l1）被读取，发生在配置加载**之前**。换句话说，它们决定的是「git 读取哪个仓库」，而不是「git 读到了什么配置」。

2. **配置型环境变量**（`GIT_CONFIG_PARAMETERS`、`GIT_CONFIG_COUNT` 配合 `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`）。
   它们本身就是配置值，只是载体不是文件而是环境变量。它们在配置加载阶段（`config.c`）被读取，优先级**最高**。

这两类变量还有一个共同属性：它们大多是「仓库局部」的——当 git spawn 一个进入**另一个**仓库的子进程时，要主动清除它们，否则子进程会误用父仓库的上下文。承担这张「清理清单」的就是 `local_repo_env[]`。

#### 4.1.2 核心流程

读取环境变量的通用套路有三种，按「用途」对号入座：

```text
1. 直接 getenv（拿原始字符串）
   示例：get_git_namespace() 读 GIT_NAMESPACE

2. git_env_bool(name, default)（当布尔读，缺省返回 default）
   示例：use_optional_locks() 读 GIT_OPTIONAL_LOCKS
   示例：git_config_system()    读 GIT_CONFIG_NOSYSTEM

3. getenv_safe(argv, name)（strdup 一份，防 getenv 返回的静态缓冲被覆盖）
   用于值要长期持有的场合
```

`git_env_bool` 是最常用的「环境开关」读法：变量不存在则用默认值，存在但写错（不是 true/false/yes/no/…）就 `die`。

#### 4.1.3 源码精读

环境变量名全部集中登记在 `environment.h` 开头，注意它们的分组——上下文型（`GIT_DIR`/`GIT_WORK_TREE`/对象库/索引）与配置型（`GIT_CONFIG*`）混在同一份表里，靠宏名区分：

[environment.h:8-23](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h#L8-L23) — 这段定义了 `GIT_DIR`、`GIT_COMMON_DIR`、`GIT_WORK_TREE`、`GIT_OBJECT_DIRECTORY`、`GIT_INDEX_FILE`，以及本讲的主角 `GIT_CONFIG`、`GIT_CONFIG_PARAMETERS`、`GIT_CONFIG_COUNT`，还有仓库发现用的 `GIT_CEILING_DIRECTORIES`。每个宏的注释提示了它的用途。

`local_repo_env[]` 在 `environment.h` 里先声明、在 `environment.c` 里给值。这个数组就是「子进程切到别的仓库时要清除的环境变量清单」：

[environment.c:95-112](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c#L95-L112) — 数组里列出了所有「仓库局部」的环境变量，末尾以 `NULL` 收尾方便遍历。注意它同时包含上下文型（`GIT_DIR`、`GIT_OBJECT_DIRECTORY`、`GIT_INDEX_FILE` …）和配置型（`CONFIG_ENVIRONMENT`、`CONFIG_DATA_ENVIRONMENT`、`CONFIG_COUNT_ENVIRONMENT`）两类。

`git_env_bool` 的实现简单而典型——存在性、可解析性、默认值三件事一次处理：

[parse.c:197-208](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse.c#L197-L208) — `getenv` 拿不到就用 `def`，拿到了就交给 `git_parse_maybe_bool` 判断布尔语义，解析失败直接 `die`。`use_optional_locks()`（[environment.c:208-211](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c#L208-L211)）和 `git_config_system()`（[config.c:1539-1542](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1539-L1542)）都是对它的一行包装。

`getenv_safe` 解决的是一个 C 语言老坑：`getenv` 返回的字符串可能指向会被后续调用覆盖的缓冲。需要长期持有值时，先 `strdup` 进一个 `strvec`（随 `argv` 一起回收）：

[environment.c:114-123](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c#L114-L123) — 它在 `git_config_from_parameters` 读 `GIT_CONFIG_KEY_n` 时被复用（见 4.3）。

#### 4.1.4 代码实践

**目标**：用 `git_env_bool` 驱动的两个开关，亲手验证「环境变量 = 进程级一次性开关」。

**步骤**：

1. 在任意 git 仓库里运行：
   ```bash
   git config --get core.notesRef        # 通常无输出
   GIT_CONFIG_NOSYSTEM=1 git status      # 跳过系统级配置
   ```
2. 再对比「可选锁」开关：默认 `git fetch` 会尝试用索引锁加速，把它关掉：
   ```bash
   GIT_OPTIONAL_LOCKS=0 git fetch --dry-run
   ```

**需要观察的现象**：这两个变量都没有写进任何配置文件，`git config --list` 里也看不到它们，但它们确实改变了 git 的行为。

**预期结果**：环境变量是「不落盘」的覆盖，只在当前进程（及其子进程）生效，进程退出即消失。

> 本地是否能看到 `GIT_CONFIG_NOSYSTEM` 真正跳过了 `/etc/gitconfig`，取决于你机器上是否存在该文件；若无则无可观察差异——这一点**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`local_repo_env[]` 里为什么既有 `GIT_DIR` 又有 `GIT_CONFIG_PARAMETERS`？它们属于同一类吗？

**参考答案**：不属于同一类，但都是「仓库局部」状态。`GIT_DIR` 是上下文型（指向某个具体仓库），`GIT_CONFIG_PARAMETERS` 是配置型（携带 `-c` 注入的值）。把它们一起放进清除清单，是为了让 git 在 spawn 一个进入**别的**仓库的子进程时，既不误用父仓库的路径上下文，也不把父仓库的 `-c` 注入泄漏过去。

**练习 2**：为什么 git 推荐用 `git_env_bool` 而不是直接 `getenv` 来读布尔开关？

**参考答案**：`getenv` 只能告诉你「变量在不在」，不能判断 `GIT_OPTIONAL_LOCKS=maybe` 这种非法值。`git_env_bool` 统一了布尔语义（true/false/yes/no/on/off/1/0/空串），并在非法值时 `die`，避免「静默地当成默认值」导致难以排查的行为偏差。

### 4.2 配置优先级链

#### 4.2.1 概念说明

把 4.1 的两类变量，连同 u6-l1 讲过的配置文件，按 git **实际读取的先后**排成一队，就得到完整的「优先级链」。铁律还是那一句：**后读到的覆盖先读到的**。

完整链（从低到高）：

```text
┌─ 系统级   /etc/gitconfig            （CONFIG_SCOPE_SYSTEM）
│  └─ 可被 GIT_CONFIG_NOSYSTEM=1 整段跳过
├─ 全局级   ~/.config/git/config(XDG) （CONFIG_SCOPE_GLOBAL）
├─ 全局级   ~/.gitconfig               （CONFIG_SCOPE_GLOBAL）
├─ 仓库级   .git/config                （CONFIG_SCOPE_LOCAL）
├─ 工作树级 .git/config.worktree       （CONFIG_SCOPE_WORKTREE，需显式启用）
└─ 命令级   GIT_CONFIG_PARAMETERS /    （CONFIG_SCOPE_COMMAND，最高优先级）
            GIT_CONFIG_COUNT + KEY/VALUE
```

注意几个要点：

- **命令级（COMMAND）是配置体系里的最高优先级**，它由 `-c`/`--config-env`（见 4.3）或 `GIT_CONFIG_*` 环境变量提供，**最后**被读取。
- 但「命令级」之上还有一类更霸道的覆盖：**上下文型环境变量**（如 `GIT_DIR`）。它不是配置项，根本不进这条链——它直接改写了「git 在读哪个仓库的 local/worktree 配置」，是更底层的覆盖。
- 每条配置值在被解析时，都会被打上 `scope`（作用域）和 `origin_type`（来源类型）两个标签，存进 `struct key_value_info`。`git config --show-origin` 能看到这些标签，正是依赖它。

#### 4.2.2 核心流程

`do_git_config_sequence` 是优先级链的「裁判」，它严格按上面的顺序，依次把每个来源喂给同一个回调 `fn`：

```text
do_git_config_sequence(fn):
    if git_config_system():              # 读 GIT_CONFIG_NOSYSTEM
        fn <- system_config   (SYSTEM)
    fn <- xdg_config         (GLOBAL)
    fn <- user_config        (GLOBAL)
    fn <- repo_config        (LOCAL)
    if worktree_config 启用:
        fn <- worktree_config (WORKTREE)
    fn <- git_config_from_parameters()   # 读 GIT_CONFIG_* (COMMAND)  ← 最后
```

因为 `fn` 内部（最终是 `config_set` 的 hashmap/list）对**同一键的多次写入采取「后写覆盖先写」**，所以「读取顺序 = 优先级从低到高」这条等式成立。

#### 4.2.3 源码精读

作用域与来源类型这两个枚举，是整条优先级链的「坐标系」：

[config.h:39-64](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L39-L64) — `enum config_scope` 列出 SYSTEM/GLOBAL/LOCAL/WORKTREE/COMMAND/SUBMODULE 六档作用域；`enum config_origin_type` 区分值来自文件、标准输入、blob 还是命令行（`CONFIG_ORIGIN_CMDLINE`）。每条值都用 [config.h:119-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L119-L131) 的 `struct key_value_info` 携带这两个标签。

优先级链的实体——注意每一档调用都带上了对应的 `scope`，并且**命令级被放在函数最后**：

[config.c:1571-1602](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1571-L1602) — 逐段看：系统级受 `git_config_system()`（即 `GIT_CONFIG_NOSYSTEM`）门控；XDG 与 `~/.gitconfig` 同属 GLOBAL；仓库级 `config` 是 LOCAL；`config.worktree` 只有在 `repository_format_worktree_config` 开启时才读，属 WORKTREE；最后一行 `git_config_from_parameters(fn, data)`（[config.c:1601](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1601)）把命令级读进来，**位置最靠后，故优先级最高**。

#### 4.2.4 代码实践

**目标**：用 `--show-origin` 直接「看见」优先级链上的每一档来源。

**步骤**：

1. 在一个仓库里运行：
   ```bash
   git config --list --show-origin --show-scope
   ```
2. 分别向 system / global / local 写入同一个键，再观察谁赢：
   ```bash
   # local（默认）
   git config user.name "Local Me"
   # 临时用 -c 覆盖（命令级）
   git -c user.name="Command Me" config --get user.name
   ```

**需要观察的现象**：第 1 步输出的每行行首会标注 `file:/etc/gitconfig`、`file:/home/you/.gitconfig`、`file:.git/config` 以及作用域 `system/global/local`。第 2 步里，`-c` 的值盖过了 local 的值。

**预期结果**：`git -c user.name="Command Me"` 输出 `Command Me`，证明命令级 > 仓库级。

> 系统级与全局级文件是否存在、是否可写，依你机器而定；若不存在则链上相应档位为空。具体文件路径**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：假设 `~/.gitconfig` 里写了 `user.name=Alice`，`.git/config` 里写了 `user.name=Bob`，又用 `git -c user.name=Carol` 运行命令。最终 `user.name` 是谁？为什么？

**参考答案**：是 `Carol`。三处分别属 GLOBAL、LOCAL、COMMAND，按读取顺序 system→global→local→command，COMMAND 最后读、覆盖前者，故 `Carol` 胜出。这也说明 `-c` 是「不改任何文件就能临时覆盖」的最高优先级手段。

**练习 2**：`GIT_DIR=/path/to/other.git git config --get user.name` 会读哪个仓库的 local 配置？这属于优先级链上的一档吗？

**参考答案**：会读 `/path/to/other.git/config`。它**不属于**优先级链上的某一档——`GIT_DIR` 是上下文型环境变量，直接改写了「当前仓库是哪个」，从而间接决定了 LOCAL 档指向哪个文件。它是比整条配置链更底层的覆盖。

### 4.3 -c 与 --config-env 注入

#### 4.3.1 概念说明

`-c` 是日常最常用的「临时改配置」手段：`git -c user.name=Test commit`。但它的内部实现很有意思——**`-c` 并不是在解析命令行时直接把值塞进配置表，而是先把值「翻译」成一个环境变量 `GIT_CONFIG_PARAMETERS`，留给后续配置加载阶段去读**。这样做有两个好处：

1. **统一通道**：`-c` 注入的值与 `GIT_CONFIG_*` 环境变量走的是同一条「命令级」读取路径（4.2 里最后那一档），代码不需要为 `-c` 单写一套逻辑。
2. **跨子进程传播**：git 经常 spawn 子进程（如 `git push` 会调 `git send-pack`）。把 `-c` 值放进环境变量，子进程自动继承，命令级覆盖就能一路传下去。

`--config-env key=ENVNAME` 是 `-c` 的「安全变体」：值不是直接写在命令行上，而是从某个环境变量里取。为什么需要它？因为**命令行参数对同机其他用户可见（`ps` 能看到）**，把令牌、密码写在 `-c token=secret` 上不安全。改成 `--config-env=http.extraheader=MYTOKEN`，值只存在环境变量里，`ps` 看不到。

两者的区别一句话：**`-c` 把「值」搬上命令行；`--config-env` 把「值」留在环境变量，命令行上只写「去哪个环境变量取」**。

#### 4.3.2 核心流程

```text
git -c KEY=VALUE subcommand
  │
  ├─ git.c: handle_options() 识别到 "-c"
  │     └─ git_config_push_parameter("KEY=VALUE")
  │           └─ git_config_push_split_parameter():
  │                 把 'KEY'='VALUE' 经 shell 单引号转义后，
  │                 追加进环境变量 GIT_CONFIG_PARAMETERS
  │
  ├─（随后）子命令运行，触发配置加载 do_git_config_sequence()
  │     └─ 最后一步 git_config_from_parameters():
  │           读 GIT_CONFIG_COUNT + GIT_CONFIG_KEY_n/VALUE_n
  │           再读 GIT_CONFIG_PARAMETERS（单引号解引）
  │           每条值打标 scope=COMMAND, origin=CMDLINE
  │
  └─ 命令级值覆盖所有配置文件 → 生效
```

关键点：`-c` 的「写入」（push）发生在 `git.c` 的选项处理阶段，而「读取」（from_parameters）发生在稍后的配置加载阶段——两者被时间错开，靠环境变量这个「中转站」连接。这正是它优先级最高（最后被读）的原因。

#### 4.3.3 源码精读

入口在 `git.c` 的 `handle_options`，它在 `cmd_main` 里、子命令分发**之前**被调用，所以 `-c` 的值一定会先落进环境变量：

[git.c:953-956](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L953-L956) — `cmd_main` 先剥掉程序名，再调用 `handle_options(&argv, &argc, NULL)` 吃掉所有全局选项。

`handle_options` 里 `-c` 与 `--config-env` 的处理，以及 `--git-dir`、`-C` 等「上下文型」选项的对比：

[git.c:264-281](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L264-L281) — `-c` 调 `git_config_push_parameter`，`--config-env`（含 `=` 紧凑形式）调 `git_config_push_env`。对比 [git.c:214-227](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L214-L227) 的 `--git-dir` 直接 `setenv(GIT_DIR_ENVIRONMENT,…)`、[git.c:312-324](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L312-L324) 的 `-C` 直接 `chdir`——你会清楚看到两类选项的差别：上下文型选项当场改环境/进程状态，而 `-c` 只是把值「寄存」进 `GIT_CONFIG_PARAMETERS`。

`git_config_push_parameter` 负责「拆键值 + 转义 + 追加」：

[config.c:466-500](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L466-L500) — 找第一个 `=` 拆分（注释解释了为何取第一个 `=`：保护值里的 `=`，因为值更可能含不可信输入）。无 `=` 时当作「布尔型键」（隐式 true）。真正的「追加进环境变量」在 [config.c:450-464](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L450-L464) 的 `git_config_push_split_parameter`：用 `sq_quote_buf` 对键和值做 shell 单引号转义，拼成 `'key'='value'` 追加到 `GIT_CONFIG_PARAMETERS`，多个 `-c` 之间用空格分隔。

`--config-env` 走的 `git_config_push_env`，差别在于「值来自另一个环境变量」：

[config.c:502-524](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L502-L524) — 解析 `key=ENVNAME`，用 `getenv(ENVNAME)` 取值（取不到就 `die`），再复用同一个 `git_config_push_split_parameter` 落进 `GIT_CONFIG_PARAMETERS`。所以 `-c` 与 `--config-env` 最终殊途同归，都变成 `GIT_CONFIG_PARAMETERS` 里的条目。

读取端 `git_config_from_parameters` 同时支持两种外部协议：编号式（`GIT_CONFIG_COUNT` + `GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`，便于脚本批量注入）与单引号串式（`GIT_CONFIG_PARAMETERS`，`-c` 用的就是它）：

[config.c:731-797](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L731-L797) — 先读 `GIT_CONFIG_COUNT` 循环取成对键值，再读 `GIT_CONFIG_PARAMETERS` 交给 `parse_config_env_list`（[config.c:679-729](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L679-L729)）做单引号解引。每条值经 [config.c:641-648](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L641-L648) 的 `kvi_from_param` 打上 `origin_type=CONFIG_ORIGIN_CMDLINE`、`scope=CONFIG_SCOPE_COMMAND`——这正是 4.2 里「命令级」标签的来源。

#### 4.3.4 代码实践

**目标**：亲手走一遍「`-c` → 环境变量 → 配置」的完整链路，并体会 `--config-env` 的安全意义。

**步骤**：

1. **观察 `-c` 如何变成环境变量**。`-c` 的值会被 propagate 给子进程，所以用一个会 spawn 子进程的命令就能看到它：
   ```bash
   git -c user.name=Test -c user.email=t@e.x show-branch 2>/dev/null
   # 或更直接：让 git 自带的子进程回显
   GIT_TRACE=1 git -c core.editor=nanO commit 2>&1 | grep -i param || true
   ```
2. **直接用环境变量复刻 `-c`**，证明二者等价（这条命令等价于 `git -c user.name=Test config --get user.name`）：
   ```bash
   GIT_CONFIG_PARAMETERS="'user.name=Test'" git config --get user.name
   ```
3. **体验 `--config-env` 的「值不在命令行」**：
   ```bash
   MYTOKEN="x-secret-y"
   git --config-env=http.extraheader=MYTOKEN config --get http.extraheader
   # 此时 ps 里只能看到 --config-env=http.extraheader=MYTOKEN，看不到 x-secret-y
   ```

**需要观察的现象**：
- 第 2 步应输出 `Test`，说明 `GIT_CONFIG_PARAMETERS` 与 `-c` 走的是同一条命令级通道。
- 第 3 步应输出 `x-secret-y`，说明值确实从环境变量 `MYTOKEN` 取到了。

**预期结果**：第 2 步输出 `Test`，第 3 步输出 `x-secret-y`。

> 第 1 步是否能从 `GIT_TRACE` 输出里抓到 `GIT_CONFIG_PARAMETERS` 字样，取决于具体子命令是否打印环境；若抓不到，可直接用第 2 步验证等价性。具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`git -c foo.bar=1 -c foo.bar=2 config --get foo.bar` 会输出什么？结合 `git_config_push_split_parameter` 说明。

**参考答案**：输出 `2`。两次 `-c` 各自被单引号转义后**追加**进同一个 `GIT_CONFIG_PARAMETERS`，形成 `'foo.bar=1' 'foo.bar=2'`。读取时按顺序回调，后写的 `2` 覆盖先写的 `1`，故得 `2`。

**练习 2**：为什么 CI/CD 里用 `git --config-env=http.extraheader=TOKEN_VAR` 比把令牌塞进 `git -c http.extraheader=$TOKEN` 更安全？

**参考答案**：`-c` 的值会出现在进程命令行上，同机其他用户（或能读进程列表的日志/监控）可通过 `ps` 看到。`--config-env` 只把「变量名」写上命令行，真实令牌留在环境变量里，进程参数不暴露明文。两者最终都进 `GIT_CONFIG_PARAMETERS`，但 `--config-env` 把「明文何时出现」推迟并限制在了进程环境这一更受限的可见范围。

**练习 3**：若想用脚本一次性注入 10 个配置项，除了写 10 个 `-c`，还有什么方式？提示：看 `git_config_from_parameters` 先读哪个变量。

**参考答案**：用编号式协议：设 `GIT_CONFIG_COUNT=10`，再设 `GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` … `GIT_CONFIG_KEY_9`/`GIT_CONFIG_VALUE_9`。`git_config_from_parameters`（[config.c:741-780](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L741-L780)）会先于 `GIT_CONFIG_PARAMETERS` 处理它们，更适合程序化批量注入，免去单引号转义的麻烦。

## 5. 综合实践

把本讲三个模块串起来，完成一个「在不改任何配置文件的前提下，临时让 git 在另一个目录里、用另一个身份、带一个秘密 HTTP 头运行」的任务。

**场景**：你有一个备用仓库 `/tmp/other.git`（若没有，先 `git init --bare /tmp/other.git`），想在**不修改它的 config、也不修改你的全局 config** 的情况下，用一次性身份和令牌访问它。

**操作**：

```bash
# 1. 准备一个秘密令牌（只放环境变量，不落盘、不上命令行）
export MYTOKEN="Bearer super-secret-$(date +%s)-token"

# 2. 一次性组合三种覆盖：
#    GIT_DIR          → 上下文型：切换到别的仓库（不改仓库 config）
#    -c user.name=... → 命令级：临时身份
#    --config-env     → 命令级：从 MYTOKEN 取秘密 HTTP 头
GIT_DIR=/tmp/other.git \
git -c user.name="Temp Bot" \
    -c user.email="bot@example.invalid" \
    --config-env=http.extraheader=MYTOKEN \
    config --show-origin --show-scope --get-regexp '^(user|http)\.'
```

**预期观察**：

- 输出的每行行首都标注 `command line`（来源）和 `command`（作用域），证明这些值都是命令级注入、没有写进任何文件。
- `user.name`/`user.email` 来自 `-c`，`http.extraheader` 来自 `MYTOKEN` 环境变量。
- 仓库 `/tmp/other.git/config` 与你的 `~/.gitconfig` 都**没有被修改**（可用 `git -C /tmp/other.git log -- config` 或直接 `cat` 确认）。

**思考**：把上面的命令换成它的「等价纯环境变量版」该怎么写？即用 `GIT_DIR` + `GIT_CONFIG_PARAMETERS` 复刻相同效果（提示：注意 `http.extraheader` 的值要先放进某个变量，再以 `'http.extraheader=<值>'` 形式拼进 `GIT_CONFIG_PARAMETERS`）。这能帮你彻底理解「`-c` 只是 `GIT_CONFIG_PARAMETERS` 的语法糖」。

## 6. 本讲小结

- git 的环境变量分两类：**上下文型**（`GIT_DIR`/`GIT_WORK_TREE`/对象库/索引等，在仓库发现阶段读取，决定「读哪个仓库」）与**配置型**（`GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT`，在配置加载阶段读取，决定「读到什么值」）。
- 配置优先级链按读取顺序为 system → global(XDG) → global(~) → local → worktree → **command**，后读覆盖先读；命令级最高。
- `-c KEY=VALUE` 并非直接写配置表，而是经 `git_config_push_parameter` 转义追加进 `GIT_CONFIG_PARAMETERS`，留待配置加载最后一步 `git_config_from_parameters` 读取——这就是它优先级最高的原因。
- `--config-env key=ENVNAME` 与 `-c` 殊途同归（都进 `GIT_CONFIG_PARAMETERS`），但值来自环境变量，避免明文出现在命令行（`ps` 可见），适合放令牌。
- 每条配置值都带 `scope` 与 `origin_type` 标签（`struct key_value_info`），命令级被打成 `CONFIG_SCOPE_COMMAND` / `CONFIG_ORIGIN_CMDLINE`，`git config --show-origin --show-scope` 可见。
- `local_repo_env[]` 是「仓库局部」环境变量清单，git spawn 进入别的仓库的子进程时会清除它们，避免上下文与命令级注入跨仓库泄漏。

## 7. 下一步学习建议

- **往下读**：配置最终被消费的地方散落在各处——`git_default_core_config`（[environment.c:301-555](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c#L301-L555)）展示了 `core.*` 配置项如何落成 `struct repo_config_values` 与一堆全局变量，是观察「配置值如何影响行为」的好样本。
- **往横向读**：回到 u6-l1 没展开的 `read_early_config`（[config.c:1675](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1675)），它在仓库发现尚未完成时就需要配置（例如读 `init.defaultBranch`），是优先级链在「早期阶段」的一个特殊入口。
- **进入下一个子系统**：配置讲完后，git 的运行时上下文（`struct repository` 的 `gitdir`/`settings`/`hash_algo`）就已经齐备。建议进入 u7「版本遍历 revision walk」，看 `revision.c` 如何基于这些上下文开始遍历提交图。
- **动手验证**：本讲多处结果依赖你的本地环境（系统/全局配置文件是否存在、子命令是否打印环境），建议照「代码实践」逐条跑一遍，把「待本地验证」的位置补成你机器上的真实输出。
