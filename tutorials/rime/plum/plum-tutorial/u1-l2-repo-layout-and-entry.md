# 目录结构与脚本入口

## 1. 本讲目标

上一篇我们已经知道 plum 是 Rime 的配置管理器，并理清了 schema / dictionary / package / recipe 四个概念。本讲带着你打开 plum 仓库，从「文件」的视角建立整体地图，重点解决三个问题：

1. 仓库里都有哪些文件？每个文件各负责什么？
2. 那条 `curl ... | bash` 一行命令，到底是怎么跑起来的？为什么同一个 `rime-install` 脚本既能在「还没 clone 过 plum」的机器上跑，也能在「已经 clone 好的工作副本」里跑？
3. 真正的核心安装逻辑藏在哪个目录？

学完本讲，你应该能画出 `rime-install` 启动阶段的判定流程图，并能说出 `scripts/` 目录下每个脚本的一句话职责。

## 2. 前置知识

- **shell 脚本基础**：知道 `if/then/fi`、变量 `${var}`、`source`（在 bash 里等价于 `.`）加载另一个脚本的意思。
- **环境变量**：plum 大量用环境变量做配置（如 `plum_repo`、`plum_dir`、`rime_dir`），需要理解「未设置」(`-z`) 和「已设置」(`-n`) 的判断。
- **`readlink -f`**：把可能带符号链接、相对路径的脚本路径解析成绝对真实路径。
- **`git clone --depth 1`**：浅克隆，只取最近一次提交，plum 用它来快速获取依赖包。
- 承接 [u1-l1](u1-l1-project-overview.md)：你已经知道 package 对应一个 GitHub 仓库、recipe（℞）描述安装动作。本讲不重复这些概念，而是看「代码长什么样」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md) | 项目说明文档 | 给出目录的整体定位与用法 |
| [rime-install](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install) | bash 主入口脚本（共 62 行） | 自举、转发、加载模块、进入安装循环 |
| [scripts/bootstrap.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh) | 模块加载系统 | 入口处被 `source`，提供 `require` |
| [scripts/](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts) | 全部核心逻辑所在目录 | 一句话职责速查 |

## 4. 核心概念与源码讲解

### 4.1 仓库文件总览

#### 4.1.1 概念说明

plum 仓库的体量很小——用 `git ls-files` 列出来只有二十多个文件，而且**没有编译产物、没有 `node_modules`、没有第三方依赖目录**。这是因为 plum 的本质就是「一组 shell 脚本 + 一份包清单」，运行时依赖只有系统里的 `git` 和 `bash`。

整个仓库的文件可以归成四类：

1. **入口脚本**：`rime-install`（bash 版）、`rime-install.bat` 系列（Windows 版）。
2. **核心逻辑**：`scripts/` 目录下的全部 `.sh` 文件。
3. **包清单**：`preset-packages.conf` / `extra-packages.conf` / `all-packages.conf`（及其 `.bat` 对应物），定义「预设里都装哪些包」。
4. **工程化文件**：`Makefile`（把 Rime 数据当系统软件来构建安装）、`LICENSE`、`README.md`、`.gitattributes`、`.gitignore`。

#### 4.1.2 核心流程

仓库里文件的分工可以这样一览：

```text
plum/
├── README.md                        # 项目说明（也是面向用户的「说明书」）
├── LICENSE                          # LGPLv3
├── Makefile                         # Unix 下「作为共享数据」构建安装
├── rime-install                     # ★ bash 主入口（本讲主角）
│
├── rime-install.bat                 # Windows 批处理安装器
├── rime-install-bootstrap.bat       # Windows 初始引导（下载 .bat、建快捷方式）
├── rime-install-config.bat          # Windows 配置（plum_dir / rime_dir / use_plum）
│
├── preset-packages.conf  / .bat     # :preset 集合
├── extra-packages.conf   / .bat     # :extra  集合
├── all-packages.conf     / .bat     # :all    集合（preset + extra）
│
├── scripts/                         # ★ 全部核心逻辑
│   ├── bootstrap.sh                 #   模块加载系统（require/provide）
│   ├── styles.sh                    #   终端配色与统一输出函数
│   ├── resolver.sh                  #   配方字符串解析（user/repo@branch:recipe）
│   ├── install-packages.sh          #   安装主循环
│   ├── frontend.sh                  #   前端识别与 rime_dir 猜测
│   ├── fetch-package.sh             #   浅克隆下载包
│   ├── update-package.sh            #   更新已下载的包
│   ├── recipe.sh                    #   recipe.yaml 执行引擎
│   ├── selector.sh                  #   --select 交互菜单
│   ├── minimal-build.sh             #   精简版数据裁剪
│   └── split-packages.sh            #   仓库拆分维护工具（一次性脚本）
│
└── .gitignore                       # 忽略 /package /output plum-*.tar.gz
```

> 说明：上面这棵树是把 `git ls-files` 的真实结果按职责重新分组画的，目录下文件名都是实际存在的。

`.gitignore` 里忽略的三项也值得注意，它们揭示了运行/构建过程会产生哪些**临时产物**：

- `/package`：克隆下来的各个输入方案包的暂存目录。
- `/output`：`Makefile` 构建时的输出目录。
- `plum-*.tar.gz`：`make dist` 打出的源码包。

#### 4.1.3 源码精读

`git ls-files` 列出的就是仓库里全部被追踪的文件。我们直接看根目录的脚本与清单文件：

- 主入口只有 62 行：
  [rime-install:1-62](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L1-L62) —— 整个 bash 版安装器的全部内容，本讲下半段会逐段拆解。

- 包清单是「被 source 进来的 shell 片段」，不是 JSON/YAML。例如 `preset-packages.conf` 里定义的是一个名为 `package_list` 的 bash 数组。这点很关键：**清单文件本身也是可执行的 shell 代码**，由 `resolver.sh` 在运行时 `source` 进来。Windows 侧的 `preset-packages.bat` 则用 `%package_list%` 表达同样的内容，形成「.conf 与 .bat 双实现」。

- `.gitignore` 只忽略三类临时产物：
  [.gitignore:1-3](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/.gitignore#L1-L3)。

#### 4.1.4 代码实践

1. **实践目标**：用一条只读命令建立「文件 → 职责」的直觉，不靠死记。
2. **操作步骤**：在仓库根目录执行
   ```bash
   git ls-files
   ```
   把输出和上面 4.1.2 的目录树对照，给每个文件标注一个一句话职责。
3. **观察现象**：你会发现全部核心逻辑都集中在 `scripts/` 下，根目录的 `.sh` 只有 `rime-install` 一个；其余根目录文件要么是清单，要么是 Windows 批处理。
4. **预期结果**：你得到一张「文件清单 + 职责」对照表，后续读源码时随时可以回查。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `preset-packages.conf` 看起来像配置文件，却被放在仓库根目录、还能被 bash 直接 `source`？

> **答案**：因为它本质就是一段 shell 代码——定义了 `package_list` 数组。`resolver.sh` 用 `source` 加载它，从而拿到「预设里有哪些包」。把它叫做 `.conf` 只是表达「这是一份清单配置」，但格式是合法的 shell 语法。

**练习 2**：`.gitignore` 里 `/package` 和 `/output` 为什么以 `/` 开头？

> **答案**：前导 `/` 锚定仓库根目录，表示只忽略根目录下的 `package`、`output` 目录，而不影响子目录里同名的路径（比如某个包内部若也有 `output` 目录不会被误伤）。

---

### 4.2 rime-install 自举与转发逻辑

#### 4.2.1 概念说明

`rime-install` 最巧妙的地方在于它是**自举（bootstrap）**的：同一个脚本要同时应付两种截然不同的运行场景，且都能正确进入安装流程。

- **场景 A：curl 管道运行**。用户执行
  ```sh
  curl -fsSL https://raw.githubusercontent.com/rime/plum/master/rime-install | bash
  ```
  这时 `rime-install` 被 `curl` 下载到管道里直接喂给 `bash` 执行，**本地根本没有 plum 仓库**，`scripts/` 目录更不存在。脚本必须自己先把 plum 克隆下来。

- **场景 B：在工作副本里运行**。用户已经
  ```sh
  git clone --depth 1 https://github.com/rime/plum.git
  cd plum
  bash rime-install :preset
  ```
  这时 `scripts/` 就在当前目录下，脚本不需要再克隆，直接干活即可。

为了用一个脚本同时覆盖这两种场景，作者用了一个**「判定 + 转发」**的小技巧：先判断「我现在是不是已经在一个完整的工作副本里」，如果不是，就把自己克隆下来，然后**重新调用克隆好的那份更新的 `rime-install`**，自己优雅退出。这正是 README 里那句「This is equivalent to cloning this repo and running the local copy」背后真正发生的事。

> 名词解释：**自举（bootstrap）**——程序靠自身能力把自己从「不存在/不完整」拉到「可运行」的状态，像「拎着自己的鞋带把自己提起来」。**重入 / 转发（re-entrancy / forwarding）**——脚本执行到一半，再次调用「另一个版本的自己」，并把参数原样传过去。

#### 4.2.2 核心流程

前 24 行的判定流程（本讲实践任务要画的就是这张图）：

```text
                ┌─────────────────────────────────────────────┐
 start          │ 1. plum_repo 未设置?  → 默认 'rime/plum'      │
                └────────────────────┬────────────────────────┘
                                     ▼
                ┌─────────────────────────────────────────────┐
                │ 2. plum_dir 未设置?                          │
                │    是 → plum_dir = 本脚本所在目录(readlink -f) │
                │         该目录里没有 scripts/install-packages.sh?
                │            是 → plum_dir = 'plum'   # 不在工作副本 │
                └────────────────────┬────────────────────────┘
                                     ▼
                ┌─────────────────────────────────────────────┐
                │ 3. plum_dir 不存在?  → git clone --depth 1     │
                └────────────────────┬────────────────────────┘
                                     ▼
                ┌─────────────────────────────────────────────┐
                │ 4. 当前脚本 $0  与  plum_dir/rime-install      │
                │    是同一个文件吗 (-ef)?                      │
                │    否 → 执行 plum_dir/rime-install "$@" 然后 exit│  ← 转发
                │    是 → 继续往下（在工作副本里，真正干活）        │
                └─────────────────────────────────────────────┘
```

关键直觉：

- 第 2 步用「**目录里有没有 `scripts/install-packages.sh`**」作为「我是不是在一个完整工作副本里」的探针——这是一个非常实用的判定技巧，比检查 `.git` 目录更贴合 plum 自身的结构。
- 第 4 步用 `[[ "$0" -ef file ]]` 判断「我现在跑的这个脚本文件」和「工作副本里的 `rime-install`」是不是物理上的同一个文件。`-ef` 比较的是 inode，能正确处理符号链接和相对路径。**curl 管道场景下两者一定不是同一个文件**（一个是管道临时内容，一个是磁盘上 clone 出来的），所以一定会走「转发」分支。

转发之后，控制权来到「已经是工作副本」的路径，开始真正的初始化：

```text
5. export root_dir=plum_dir
6. source bootstrap.sh  →  require 'styles'   # 加载模块系统与终端输出
7. rime_dir 未设置?  → require 'frontend' ; guess_rime_user_dir
8. 解析 --select / 默认 target=':preset' / 进入 for 循环逐个安装
```

#### 4.2.3 源码精读

逐段对照真实代码。

**① 默认仓库名**——如果环境没指定，就用官方仓库：
[rime-install:3-5](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L3-L5)
```bash
if [[ -z "${plum_repo}" ]]; then
    plum_repo='rime/plum'
fi
```
这行决定了「自举时从哪个仓库克隆 plum 自身」，用户可用 `plum_repo=...` 覆盖（比如用镜像）。

**② 判定当前是否已在工作副本**——这是自举的核心：
[rime-install:7-14](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L7-L14)
```bash
if [[ -z "${plum_dir}" ]]; then
    # am I in a working copy already?
    plum_dir="$(dirname "$(readlink -f "$0")")"
    if ! [[ -f "${plum_dir}"/scripts/install-packages.sh ]]; then
        # make a copy of plum in a subdirectory
        plum_dir='plum'
    fi
fi
```
注意注释 `# am I in a working copy already?` 把意图说得很清楚：先假设「我就在一个工作副本里」，用 `readlink -f "$0"` 拿到本脚本绝对路径，再取其所在目录；然后用 `scripts/install-packages.sh` 是否存在来验证这个假设，**不成立就退化成 `plum_dir='plum'`**（即在当前目录下新建一个名为 `plum` 的子目录）。

> 为什么用 `scripts/install-packages.sh` 当探针，而不是随便挑一个文件？因为它是整个安装链路上最具代表性的「核心模块」——它在，就说明整个 `scripts/` 都在，是一个完整工作副本。后续进阶讲义会专门讲它。

**③ 不存在就克隆**：
[rime-install:16-18](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L16-L18)
```bash
if ! [[ -e "${plum_dir}" ]]; then
    git clone --depth 1 "https://github.com/${plum_repo}.git" "${plum_dir}"
fi
```
`--depth 1` 浅克隆，只取最新一次提交，速度快。

**④ 转发到新版脚本**：
[rime-install:20-24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L20-L24)
```bash
if ! [[ "$0" -ef "${plum_dir}"/rime-install ]]; then
    # run the newer version of rime-install
    "${plum_dir}"/rime-install "$@"
    exit
fi
```
注释 `# run the newer version of rime-install` 点明设计意图：**磁盘上 clone 下来的那份永远被视为「更新的权威版本」**，临时跑的那份把自己当跳板，调它一次就 `exit`。`"$@"` 把所有命令行参数原样透传，保证转发是「无损」的。

**⑤ 进入工作副本路径后的初始化**：
[rime-install:26-28](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L26-L28)
```bash
export root_dir="${plum_dir}"
source "${root_dir}"/scripts/bootstrap.sh
require 'styles'
```
到这一步，`root_dir` 被导出供后续所有子脚本使用；`source bootstrap.sh` 启用 `require`/`provide` 模块加载机制（下一篇进阶讲义会详讲）；紧接着 `require 'styles'` 加载终端输出函数。

**⑥ 之后的逻辑**（先建立印象，细节留给后续讲义）：
[rime-install:30-61](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L30-L61) 依次完成：猜测 `rime_dir`（30–34）、处理 `--select`（36–39）、设定默认目标 `:preset`（41–45）、交互式选择（47–51）、以及最终的 `for target in ...` 安装主循环（53–61）。注意第 54–57 行有个特殊分支：当 target 是 `plum` 时，执行 `git pull` 来**更新 plum 自身**，这就是 README 里 `bash rime-install plum` 的实现。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「自举 + 转发」在两种场景下分别走了哪条分支，把抽象流程图落实成可观察的行为。
2. **操作步骤**：
   - **场景 A（模拟 curl 管道）**：在一个**空临时目录**里执行
     ```bash
     mkdir -p /tmp/plum-sceneA && cd /tmp/plum-sceneA
     # 直接用本地仓库里的脚本，但假装「本地没有 plum」
     bash /path/to/rime-plum/rime-install :preset
     ```
     > 注意把 `/path/to/rime-plum` 换成你机器上 plum 仓库的真实绝对路径。这里不要真的去 `curl`，以免联网；目的只是观察脚本如何发现「自己不在工作副本里」并 clone/转发。
   - **场景 B（工作副本内）**：进入真实 plum 仓库
     ```bash
     cd /path/to/rime-plum
     bash rime-install :preset
     ```
3. **观察现象**：
   - 场景 A：观察脚本是否会在当前目录下创建一个 `plum/` 子目录（因为 `plum_dir` 被设成 `'plum'`），并最终调用 `plum/rime-install`。你可以在 [rime-install:10-13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L10-L13) 处临时加一行 `echo "DEBUG plum_dir=${plum_dir}"` 来确认。
   - 场景 B：`plum_dir` 会等于本脚本所在目录，且**不会**触发第 20–24 行的转发分支。
4. **预期结果**：你能清楚指出两次运行分别命中了 4.2.2 流程图里的哪条分支。
5. 如果因为无法联网等原因不能实际运行，明确写「待本地验证」，但仍要完成「在 4.2.2 流程图上标注两条路径」的阅读练习。

#### 4.2.5 小练习与答案

**练习 1**：如果把脚本第 10 行的探针文件换成检查 `${plum_dir}/README.md` 是否存在，会有什么隐患？

> **答案**：`README.md` 太「普通」了——任何随手建的同名目录都可能有一份 `README.md`，会导致「误判成工作副本」，从而跳过克隆与转发，进入一个没有 `scripts/` 的「假工作副本」，紧接着第 27 行 `source scripts/bootstrap.sh` 就会失败。用 `scripts/install-packages.sh` 这种「核心模块」当探针才能保证「它在 = 整个仓库都在」。

**练习 2**：第 20 行 `[[ "$0" -ef "${plum_dir}"/rime-install ]]` 为什么用 `-ef` 而不是字符串相等 `$0 == .../rime-install`？

> **答案**：`$0` 可能是相对路径、绝对路径、或经过符号链接的路径，字符串比对几乎不会相等。`-ef` 比较的是两个路径最终指向的**同一个 inode（同一个物理文件）**，能正确识别「无论你怎么调用我，我跑的就是磁盘上这个文件」。这就是 README 提到「follow the symlink」能做到转发正确的原因。

**练习 3**：为什么转发分支最后一定要 `exit`？

> **答案**：转发调用 `"${plum_dir}"/rime-install "$@"` 之后，当前（临时）脚本如果不 `exit`，会继续往下执行第 26 行之后的初始化逻辑，导致同一份安装流程被**重复执行两次**（一次在转发后的工作副本里，一次在临时脚本里），产生重复 clone / 重复安装。`exit` 保证「跳板」使命完成后立即退场。

---

### 4.3 scripts 目录职责

#### 4.3.1 概念说明

记住一个结论：**`rime-install` 只负责「把 plum 拉起来、把参数接进来」，真正的活儿全部在 `scripts/` 里。** 这是一种很常见的「薄入口 + 厚模块」组织方式——入口脚本保持极短（62 行），把不同职责拆到 `scripts/` 下的各个模块，通过 `bootstrap.sh` 提供的 `require` 按需加载。

这样做的好处：

- 每个模块只解决一个问题，便于阅读和维护。
- 模块之间用 `require` 显式声明依赖，加载顺序清晰。
- 入口脚本几乎不变，新增功能只要加一个 `.sh` 模块。

#### 4.3.2 核心流程

`scripts/` 下每个模块的一句话职责（按调用时序粗略排列）：

| 文件 | 一句话职责 | 何时被加载 |
| --- | --- | --- |
| `bootstrap.sh` | 提供 `require`/`provide` 模块加载机制 | 入口直接 `source` |
| `styles.sh` | 终端配色与 `info`/`warning`/`error` 等输出函数 | `require 'styles'` |
| `frontend.sh` | 按 `OSTYPE` 猜测前端与 `rime_dir` | 需要猜 `rime_dir` 时 |
| `resolver.sh` | 解析 `user/repo@branch:recipe` 配方字符串 | 处理 target 时 |
| `selector.sh` | `--select` 交互式菜单 | 交互模式时 |
| `install-packages.sh` | **安装主循环**：决定装文件还是执行 recipe | 每个 target 都调 |
| `fetch-package.sh` | 浅克隆下载一个包 | 首次安装 |
| `update-package.sh` | 更新已下载的包到最新 | 已存在时更新 |
| `recipe.sh` | 解析并执行 `recipe.yaml` 三段动作 | 需要 recipe 时 |
| `minimal-build.sh` | 裁剪词典/方案，生成精简数据 | Makefile 精简构建 |
| `split-packages.sh` | 把单体仓库按目录重写历史、拆成子仓库（一次性维护工具） | 维护者手动运行 |

调用链的整体形状（只需建立印象，细节留给进阶讲义）：

```text
rime-install
  └─ source bootstrap.sh ── require 'styles'
  └─ (require 'frontend' → guess_rime_user_dir)
  └─ (require 'selector' → select_packages)   # 仅 --select 时
  └─ for target in "$@":
       └─ scripts/install-packages.sh   target  rime_dir
             ├─ resolver.sh      # 解析 target 字符串 / 加载 package_list
             ├─ fetch-package.sh # 或 update-package.sh
             └─ recipe.sh        # 需要执行配方时
```

#### 4.3.3 源码精读

入口加载模块系统的两行就是 `scripts/` 的「大门」：
[rime-install:26-28](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L26-L28)
```bash
export root_dir="${plum_dir}"
source "${root_dir}"/scripts/bootstrap.sh
require 'styles'
```

而 `bootstrap.sh` 提供的 `require` 很简单——核心就是「没加载过就 `source`，加载过就跳过」：
[scripts/bootstrap.sh:20-26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/bootstrap.sh#L20-L26)
```bash
require() {
    local module_name="$1"
    if grep -qF " ${module_name} " <<<" ${loaded_modules[*]} "; then return; fi
    source "${module_root_dir}/${module_name}.sh"
    if grep -qF " ${module_name} " <<<" ${loaded_modules[*]} "; then return; fi
    echo >&2 "ERROR: failed to load module '${module_name}'"
}
```
也就是说，`require 'styles'` 会去 `scripts/styles.sh` 加载，加载成功后该模块用 `provide 'styles'` 把名字登记进 `loaded_modules`，下次再 `require` 就直接返回。**模块系统的机制本身**留到进阶讲义 [u2-l1](u2-l1-module-system-and-styles.md) 详讲，本讲你只要记住：「`scripts/` 下每个 `.sh` 都是一个可被 `require` 的模块，名字就是去掉 `.sh` 后缀的文件名」。

最终主循环把每个 target 交给 `install-packages.sh`：
[rime-install:53-61](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L53-L61)
```bash
for target in "${targets[@]}"; do
    if [[ "${target}" == 'plum' ]]; then
        echo $(print_result 'Updating plum at') "'${plum_dir}'"
        (cd "${plum_dir}"; git pull)
        continue
    fi

    "${root_dir}"/scripts/install-packages.sh "${target}" "${rime_dir:-.}"
done
```
这段就是入口脚本对 `scripts/` 的唯一一次「正式委派」：除了 `plum` 这个特殊 target（更新 plum 自身）外，其余 target 一律交给 `install-packages.sh` 处理。

#### 4.3.4 代码实践

1. **实践目标**：把 `scripts/` 各模块和「一句话职责」对应起来，形成可快速回查的索引。
2. **操作步骤**：
   ```bash
   # 列出 scripts 下全部模块
   git ls-files scripts
   ```
   对照 4.3.2 的职责表，给每个文件写一句你自己的理解。
3. **观察现象**：模块数不多（共 11 个），但覆盖了「解析 / 下载 / 更新 / 安装 / 配方 / 选择 / 前端 / 构建 / 维护」全链路。
4. **预期结果**：你得到一张本地的 `scripts/` 模块索引表，后续读到任意一篇进阶讲义时，都能马上知道它在讲 `scripts/` 里的哪个文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rime-install` 要用 `require 'styles'` 加载输出函数，而不是直接把输出函数写在自己脚本里？

> **答案**：保持入口脚本的「薄」，并把「终端配色 / 输出格式」这种跨模块共用的能力集中到 `styles.sh`。任何子模块只要 `require 'styles'` 就能拿到同一套输出函数，避免重复定义、保证风格一致。

**练习 2**：`scripts/split-packages.sh` 是普通用户运行安装时会被加载的模块吗？

> **答案**：不是。它是一次性的**仓库维护工具**（用 `git filter-branch` 把单体仓库拆成多个子仓库），只在维护者做历史改写时手动运行，不在 `rime-install` 的任何调用链上。把它放在 `scripts/` 只是因为它也是 shell 脚本、方便和其它模块共享 `require 'styles'` 之类的工具。

## 5. 综合实践

把本讲三个最小模块串起来的小任务：**给 `rime-install` 的启动阶段写一份「执行轨迹说明书」**。

要求：

1. 用 `git ls-files` 生成仓库文件清单，并在旁边标注每个文件的职责分类（入口 / 核心逻辑 / 清单 / 工程化）。
2. 跟踪 [rime-install:1-25](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L1-L25)，按 4.2.2 的流程图，分别写出「curl 管道运行」和「工作副本内运行」两种情况下，脚本各会命中哪几个判定分支、最终落到哪一行真正开始安装。
3. 在 `scripts/` 目录下，找出 `rime-install` 主循环（第 53–61 行）唯一委派的目标模块，并指出它又可能进一步调用哪几个子模块（参考 4.3.2 的调用链图）。
4. （可选，待本地验证）在场景 A 的临时目录里实际跑一次，用临时加 `echo` 的方式验证你对 `plum_dir` 取值的预测。

产出：一份 markdown 笔记，包含上述四点。这份笔记将是你后续阅读任何一篇进阶讲义时的「总目录」。

## 6. 本讲小结

- plum 仓库非常精简，全部文件可归为四类：**入口脚本、`scripts/` 核心逻辑、包清单（`.conf`/`.bat`）、工程化文件**。
- `rime-install` 采用「**判定 + 转发**」实现自举：用 `scripts/install-packages.sh` 是否存在来探「是不是在工作副本里」，不是就克隆，并用 `-ef` 判断决定是否转发到磁盘上的新版脚本。
- 转发是无损的：`"$@"` 原样透传参数，转发后立即 `exit`，避免重复执行。
- 入口脚本只有 62 行，奉行「**薄入口 + 厚模块**」：真正的安装逻辑全部在 `scripts/`，通过 `bootstrap.sh` 的 `require` 按需加载。
- `scripts/` 共 11 个模块，覆盖解析、下载、更新、安装、配方、选择、前端识别、构建、维护全链路；其中 `split-packages.sh` 是维护者专用的一次性工具，不在安装调用链上。
- 清单文件（如 `preset-packages.conf`）本质是「定义 `package_list` 数组的 shell 片段」，运行时被 `source` 进来。

## 7. 下一步学习建议

- 下一篇 [u1-l3 运行安装与常用用法](u1-l3-running-and-usage.md) 会从「用户视角」讲怎么真正跑一次 `rime-install`：curl 一行命令、`:preset/:extra/:all` 预设、`<user>/<repo>@<branch>` 包名语法，以及 `rime_dir` / `rime_frontend` / `plum_repo` 等环境变量怎么定制。建议先跑通一次安装，再往下读源码。
- 想直接深入源码的读者，进入进阶单元后建议先读 [u2-l1 模块系统与终端样式约定](u2-l1-module-system-and-styles.md)，把本讲一笔带过的 `require`/`provide` 机制彻底弄懂——它是阅读 `scripts/` 所有模块的前置基础。
- 之后即可顺着调用链依次读：`resolver.sh`（解析 target）→ `install-packages.sh`（安装主循环）→ `fetch/update-package.sh`（下载与更新）→ `recipe.sh`（配方执行），与本讲 4.3.2 的调用链图一一对应。
