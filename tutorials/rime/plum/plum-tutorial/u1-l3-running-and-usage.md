# 运行安装与常用用法

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂并敢于运行 README 里的 curl 一行安装命令，并说清楚它背后到底发生了什么；
- 用 `:preset` / `:extra` / `:all` 三档预设集合、纯包名、`<user>/<repo>`、`@<branch>` 以及远程 `*-packages.conf` 五种语法，准确地告诉 `rime-install` 要装什么；
- 用 `rime_dir` / `rime_frontend` / `plum_repo` / `plum_dir` 等环境变量定制「装到哪、给哪个前端用、plum 从哪克隆、放哪」。

## 2. 前置知识

承接 u1-l2。你已经知道：

- plum 的全部安装逻辑在 `scripts/` 下，入口脚本 `rime-install` 只有 62 行，靠「自举 + 转发」兼容 curl 管道和工作副本两种场景。
- `rime-install` 先判定 `plum_dir`（是否已在工作副本），必要时浅克隆 plum，再用 `-ef` 判断要不要转发到磁盘上的新版脚本。

本讲在这些认知之上，把焦点从「脚本怎么启动」转到「用户怎么用」，重点回答两个问题：**告诉它装什么**，以及**告诉它装到哪**。

两个小术语先对齐：

- **target（目标）**：`rime-install` 命令行上每一个用空格分隔的参数，在代码里叫 target。一个 target 可以是 `:preset` 这种预设、`luna-pinyin` 这种包名，也可以是 `lotem/rime-zhung@master` 这种带分支的仓库地址。脚本最终把每个 target 逐个交给 `install-packages.sh` 处理。
- **rime_dir（Rime 用户目录）**：Rime 引擎读取用户配置的目录，比如 Linux 下 ibus-rime 默认是 `~/.config/ibus/rime`。plum 把文件装进这里，Rime 才能识别得到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `README.md` | 用法文档：一行命令、预设语法、包名语法、环境变量示例都集中在这里 |
| `rime-install` | 入口脚本：把命令行参数组装成 targets、按需猜 `rime_dir`、循环调用安装 |
| `scripts/frontend.sh`（辅助） | 当 `rime_dir` 未指定时，按 OSTYPE / 前端猜测 `rime_dir` |
| `preset-packages.conf`（辅助） | 定义 `:preset` 到底包含哪些包 |
| `scripts/bootstrap.sh`（辅助） | 提供 `require` / `provide` 模块加载机制，是本讲实践里需要 source 的前置文件 |

## 4. 核心概念与源码讲解

### 4.1 一行安装命令

#### 4.1.1 概念说明

README 给出的「一键安装」是这样一行：

```sh
curl -fsSL https://raw.githubusercontent.com/rime/plum/master/rime-install | bash
```

它做的事情是：用 curl 把 GitHub 上 `rime-install` 这个脚本的**文本内容**拉下来，直接通过管道交给 bash 执行。这种「curl | bash」是开源项目最常见的零安装上手方式——用户不需要先 git clone，一行命令就能跑。

关键点：被 bash 执行的是「脚本的文本」，而不是仓库里那个文件本身。所以脚本第一件事必须是**把自己放到一个稳定的工作目录里**（自举），否则它没法 `source scripts/` 下的兄弟模块。这部分机制在 u1-l2 已经讲过，本讲只关注它对外的表现：**这一行命令 ≈ 克隆 plum 后运行本地的 `bash rime-install`**。

#### 4.1.2 核心流程

把一行命令展开成它真正做的事：

1. curl 下载 `rime-install` 脚本文本 → 通过管道喂给 bash。
2. bash 执行 `rime-install`：发现不在工作副本里 → 浅克隆 plum 到 `./plum` → 转发给磁盘上的新版脚本（u1-l2 机制）。
3. 转发后的脚本加载 `scripts/` 模块。
4. 若 `rime_dir` 未设 → 调 `guess_rime_user_dir` 按 OS 猜一个默认目录。
5. 若命令行无参数 → targets 默认为 `(':preset')`。
6. for 循环把每个 target 交给 `install-packages.sh` 处理。

#### 4.1.3 源码精读

「无参数时默认装 `:preset`」这一条，是最能体现一行命令行为的代码：

[rime-install:L41-L45](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L41-L45) —— 当用户什么参数都没给（`$# -eq 0`），targets 被设为只含 `:preset` 的数组；否则 targets 就是用户传入的全部参数 `"$@"`。

[rime-install:L30-L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L30-L34) —— 当 `rime_dir` 为空时，加载 frontend 模块并调用 `guess_rime_user_dir`，它会按当前操作系统猜并 export 出一个 `rime_dir`。这就是「一行命令不需要你指定目录」的原因。

[rime-install:L53-L61](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L53-L61) —— 主循环：遍历每个 target。注意 L54-L58 有个特例——当 target 是字符串 `plum` 时，不是装包，而是 `git pull` 更新 plum 自身；其余情况交给 `install-packages.sh`。

README 对这一行命令的说明在 [README.md:L96-L105](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L96-L105)，并明确指出「它不会替你启用新方案」——这是很重要的事实边界（u1-l1 已强调）。

#### 4.1.4 代码实践

源码阅读型实践：对照 `rime-install` 画出一行命令的执行路径。

1. **实践目标**：能口述「curl | bash 之后到底发生了哪几步」。
2. **操作步骤**：阅读 `rime-install` 全文（仅 62 行），按 `L3 → L14 → L18 → L24 → L28 → L34 → L45 → L60` 的顺序，逐行写下每一步做了什么。
3. **观察重点**：L9 用 `readlink -f "$0"` 解析脚本真实路径——当通过管道执行时 `$0` 是什么？为什么这种情况下 L10 的判断会失败，从而走向 L12 把 `plum_dir` 设为字面量 `'plum'`？
4. **预期结果**：管道执行时脚本没有磁盘路径，`readlink -f "$0"` 无法定位到工作副本，于是 `plum_dir` 被设为字面量 `'plum'`，紧接着 L17 克隆 plum 到当前目录的 `plum/` 子目录。
5. **说明**：以上结论可由静态阅读代码得出，无需联网。

#### 4.1.5 小练习与答案

**练习 1**：README 说一行命令「不会启用新方案」。结合本讲，「安装」和「启用」分别指什么？

> **答案**：安装 = 把方案的源文件（`*.schema.yaml`、`*.dict.yaml` 等）拷进 `rime_dir`；启用 = 把方案 id 写进 `rime_dir/default.yaml` 的 `schema_list`。plum 只做前者，不做后者。

**练习 2**：如果不带任何参数运行 `bash rime-install`，会装什么？

> **答案**：targets 默认为 `(':preset')`，即安装 `preset-packages.conf` 里列出的 8 个包（prelude、essay、luna-pinyin、terra-pinyin、bopomofo、stroke、cangjie、quick）。

---

### 4.2 预设集合与包名语法

#### 4.2.1 概念说明

命令行上每个空格分隔的参数都是一个 target。`rime-install` 支持五种写 target 的方式，从粗到细：

1. **预设集合**：`:preset` / `:extra` / `:all`（注意冒号）。冒号开头代表「一组包」，由对应的 `*-packages.conf` 文件定义。
2. **纯包名**：`luna-pinyin`。会自动补全成 `rime/rime-luna-pinyin` 官方仓库。
3. **user/repo**：`lotem/rime-zhung`。安装第三方作者指定的仓库。
4. **带分支**：在任意包名后加 `@<branch>`，如 `jyutping@master`、`lotem/rime-zhung@master`。
5. **远程 conf 清单**：用一个 `https://.../xxx-packages.conf` 链接，或简写 `lotem/rime-forge/lotem-packages.conf`，或带分支 `lotem/rime-forge@master/lotem-packages.conf`，从别人托管在 GitHub 的清单里读取要装的包。

还有第六种「特殊 target」：字符串 `plum`。它不是装包，而是更新 plum 自身（见 4.1.3 的 L54-L58）。

#### 4.2.2 核心流程

`rime-install` 把 `"$@"` 原样塞进 targets 数组，然后 for 循环逐个处理。流程非常扁平：

```
for target in targets:
    if target == 'plum':   git pull 更新 plum 自身
    else:                  交给 install-packages.sh(target, rime_dir)
```

具体的「字符串 → 包列表」解析（比如 `:preset` 展开成哪些包、`@master` 怎么切分支）发生在 `install-packages.sh` 内部的 resolver，那是 u2 的内容。本讲只需记住一个分工：**`rime-install` 只负责收参数、循环、转发，不负责解析语法**。

#### 4.2.3 源码精读

[README.md:L111-L123](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L111-L123) —— 说明 `:preset` / `:extra` / `:all` 三档预设，并强调一行命令等价于克隆后本地运行。

[README.md:L125-L133](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L125-L133) —— 纯包名、`<user>/<repo>`、`@<branch>` 三种语法的示例。

[README.md:L135-L147](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L135-L147) —— 远程 `*-packages.conf` 的三种写法（完整 URL / `user/repo/filepath` / `user/repo@branch/filepath`）。

[rime-install:L53-L61](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L53-L61) —— 主循环。其中 [rime-install:L54-L58](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L54-L58) 是 `plum` 这个特殊 target 的处理：打印提示后执行 `(cd "${plum_dir}"; git pull)`，外层括号表示在**子 shell** 里执行，不影响当前目录；`continue` 跳过当轮 install。

`:preset` 到底装什么，看 [preset-packages.conf:L3-L12](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.conf#L3-L12)：往 `package_list` 数组里追加 8 个包名。

#### 4.2.4 代码实践

预测 target 展开结果（不联网）。

1. **实践目标**：在不实际联网的前提下，根据 `preset-packages.conf` 说出 `:preset` 会拉取哪些 GitHub 仓库。
2. **操作步骤**：读 `preset-packages.conf`，把每个包名按「`rime/rime-<包名>`」补全成 GitHub 仓库地址。例如 `luna-pinyin` → `rime/rime-luna-pinyin`，`essay` → `rime/rime-essay`。
3. **观察重点**：注意 `quick` 这个包名（`preset-packages.conf` 第 9 行）对应的是 `rime/rime-quick`，这是近期才被提升进 preset 的（见 git log 中 `promote 'quick' to preset packages` 这条提交）。
4. **预期结果**：共 8 个仓库——`rime/rime-bopomofo`、`rime/rime-cangjie`、`rime/rime-essay`、`rime/rime-luna-pinyin`、`rime/rime-prelude`、`rime/rime-quick`、`rime/rime-stroke`、`rime/rime-terra-pinyin`。
5. **待本地验证**：「`rime-` 前缀自动补全」这一步的真实拼接逻辑在 `fetch-package.sh` 中，本讲不展开，留作 u2-l5 验证。

#### 4.2.5 小练习与答案

**练习 1**：命令 `bash rime-install plum` 会做什么？和 `bash rime-install :preset` 有什么区别？

> **答案**：`plum` 是特殊 target，执行 `(cd "$plum_dir"; git pull)` 更新 plum 自身，不安装任何输入方案；`:preset` 则安装 `preset-packages.conf` 列出的 8 个包。

**练习 2**：写出「安装 `lotem/rime-zhung` 仓库的 master 分支」的命令，并说明 `@master` 在 `rime-install` 这一层被处理了吗？

> **答案**：`bash rime-install lotem/rime-zhung@master`。在 `rime-install` 这一层**没有**被解析——它只是把整个字符串当 target 原样传给 `install-packages.sh`，`@master` 的切分发生在 resolver（u2-l2）。

---

### 4.3 环境变量定制

#### 4.3.1 概念说明

`rime-install` 的行为可以用环境变量在运行前覆盖。最常用的四个：

| 变量 | 作用 | 不设置时的默认 |
| --- | --- | --- |
| `rime_dir` | 文件装到哪（Rime 用户目录） | 按 `rime_frontend` 猜（见下） |
| `rime_frontend` | 给哪个前端用 | 按 OSTYPE 猜 OS，再映射到前端 |
| `plum_repo` | 从哪个仓库克隆 plum | `rime/plum` |
| `plum_dir` | plum 工作副本放哪 | 脚本所在目录，或退化成 `./plum` |

设计思路是「能猜就猜，给用户最小负担；想精确控制就用环境变量覆盖」。

#### 4.3.2 核心流程

`rime_dir` 的确定顺序：

1. 若用户已 `export rime_dir=...`（或用 `rime_dir=... bash rime-install` 前缀赋值）→ 直接用，**不猜**。
2. 否则按 `rime_frontend` 映射到默认目录；若 `rime_frontend` 也未设 → 先按 OSTYPE 猜 frontend，再映射目录。

OSTYPE → frontend 的映射（来自 `frontend.sh`）：

| OSTYPE | frontend |
| --- | --- |
| `linux*` | ibus-rime |
| `darwin*` | squirrel（macOS） |
| `cygwin*` / `msys*` / `win*` | weasel（Windows） |

frontend → `rime_dir` 的映射（节选）：

| frontend | 默认 rime_dir |
| --- | --- |
| ibus-rime | `~/.config/ibus/rime` |
| squirrel | `~/Library/Rime` |
| weasel | `%APPDATA%\Rime` |
| fcitx-rime | `~/.config/fcitx/rime` |
| fcitx5-rime | `~/.local/share/fcitx5/rime` |

#### 4.3.3 源码精读

[rime-install:L30-L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L30-L34) —— 只在 `rime_dir` 为空时才猜目录；这意味着只要你在命令行前加 `rime_dir=...`，guess 逻辑就被完全跳过。

[rime-install:L3-L5](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L3-L5) 与 [rime-install:L7-L14](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L7-L14) —— `plum_repo` 默认 `rime/plum`；`plum_dir` 默认先尝试脚本所在目录（`readlink -f "$0"`），若不在工作副本则退化为字面量 `'plum'`。这两个变量主要用于镜像 / 分叉场景。

[scripts/frontend.sh:L9-L26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L9-L26) —— 按 OSTYPE 猜 frontend 的 `case`。

[scripts/frontend.sh:L28-L48](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L28-L48) —— 按 frontend 映射 `rime_dir` 的 `case`；L49 还会打印一行 `Installing for Rime frontend: ...` 方便用户确认。

[README.md:L149-L159](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L149-L159) —— README 给的两个实战示例：`rime_frontend=fcitx-rime bash rime-install` 和 `rime_dir="$HOME/.config/fcitx/rime" bash rime-install`。

#### 4.3.4 代码实践

本地可验证的「猜目录」实验（不联网）。

1. **实践目标**：验证 `rime_frontend` 与 `rime_dir` 的映射关系。
2. **操作步骤**：

   ```sh
   cd <plum 仓库根目录>
   unset rime_dir rime_frontend
   source scripts/bootstrap.sh   # 定义 require / provide
   source scripts/frontend.sh    # 内部会 require 'styles'
   rime_frontend=fcitx5-rime guess_rime_user_dir; echo "rime_dir=$rime_dir"
   ```

   再把 `rime_frontend` 换成 `squirrel`、`weasel` 各跑一次。
3. **观察现象**：每次都会打印 `Installing for Rime frontend: ...`，并输出 `rime_dir` 的值。
4. **预期结果**（由 `frontend.sh` L28-L48 直接得出）：
   - `fcitx5-rime` → `~/.local/share/fcitx5/rime`
   - `squirrel` → `~/Library/Rime`
   - `weasel` → `%APPDATA%\Rime`（Windows 风格路径，在 Linux 上只是字面字符串）
5. **待本地验证**：`$HOME` 的具体展开值取决于你的机器；`OSTYPE` 自动猜测（不显式设 `rime_frontend`）的行为需在你目标 OS 上实跑确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 推荐 `rime_dir="$HOME/.config/fcitx/rime" bash rime-install` 这种「前缀赋值」写法，而不是先 `export`？

> **答案**：前缀赋值（`VAR=val cmd`）只对这一条命令生效，不污染当前 shell 环境；用 `export` 则会持续影响后续命令，容易误覆盖。

**练习 2**：如果你设了 `rime_dir` 但没设 `rime_frontend`，`frontend.sh` 里的 OSTYPE 猜测还会执行吗？为什么？

> **答案**：不会。`guess_rime_user_dir` 第一步就是 `if [[ -n "${rime_dir}" ]]; then return; fi`（[frontend.sh:L6-L8](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L6-L8)）——只要 `rime_dir` 已有值，整个函数直接返回，既不猜 frontend 也不打印提示。

---

## 5. 综合实践

把「装什么 + 装到哪」串起来，在隔离目录里跑一次真实安装。

**任务**：在本地新建一个空目录，用自定义 `rime_dir` 安装 `:preset`，观察并记录产物。

**操作步骤**：

```sh
mkdir -p ~/plum-practice && cd ~/plum-practice
# 若尚未克隆 plum，先克隆（否则用 curl | bash 也行）
git clone --depth 1 https://github.com/rime/plum.git
cd plum
rime_dir=~/plum-practice/rime-test bash rime-install :preset
```

**需要观察并记录**：

1. 终端是否打印了 `Installing for Rime frontend: ...`？由于你显式设了 `rime_dir`，按 `frontend.sh` L6-L8，这行**不应**出现——验证你的预期。
2. 文件被装到了哪个目录？（预期：`~/plum-practice/rime-test/`）
3. 终端末尾会汇总类似「Updated N files.」的计数——记录 N。
4. `ls ~/plum-practice/rime-test/` 列出得到的 `*.yaml` 文件，确认包含 prelude / essay / luna-pinyin 等包的产物。

**预期结果与边界**：

- 成功时 `rime-test/` 下会出现一批 `*.yaml` 文件；plum 不会修改 `rime-test/default.yaml` 的 `schema_list`（不启用方案）。
- 这一步依赖网络（要克隆 8 个 GitHub 仓库），在无网环境会失败，属于**待本地验证**。
- 复跑第二次时，由于目标文件已存在且内容相同，`install_files` 会判定为无需更新（增量机制，u2-l3 详讲）——可以观察类似 `No files updated` 的输出。

## 6. 本讲小结

- 一行 `curl ... | bash` 等价于克隆 plum 后运行 `bash rime-install`；无参数时默认装 `:preset`，并按 OS 自动猜 `rime_dir`。
- `rime-install` 把命令行每个参数当作一个 target，for 循环逐个转发给 `install-packages.sh`，自己不做语法解析；唯一特例是 target 为 `plum` 时更新 plum 自身。
- target 有六种写法：`:preset` / `:extra` / `:all`、纯包名、`user/repo`、`@branch`、远程 `*-packages.conf`，以及特殊的 `plum`。
- `rime_dir` 决定装到哪、`rime_frontend` 决定给哪个前端、`plum_repo` / `plum_dir` 决定 plum 自身从哪来放哪；都能猜默认，也都能用环境变量覆盖。
- 重要边界：plum 只装源文件，不启用方案（不写 `default.yaml` 的 `schema_list`）。

## 7. 下一步学习建议

下一讲进入进阶层 u2。建议阅读顺序：

- 先读 **u2-l1（模块系统与 styles）**，理解 `require` / `provide` 与输出函数，因为后续所有脚本都建立在这套加载机制上。
- 再读 **u2-l2（resolver.sh）**，它会告诉你本讲里「`@master` 怎么切分、`:preset` 怎么展开成 8 个包」的真正实现。
- 想理解「装到目录」这一步到底怎么拷文件、怎么计数，看 **u2-l3（install-packages.sh 主循环）**。

继续阅读建议：`scripts/bootstrap.sh`（require / provide）、`scripts/resolver.sh`（target 解析）、`scripts/install-packages.sh`（主循环）。
