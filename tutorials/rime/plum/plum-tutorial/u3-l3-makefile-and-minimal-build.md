# 作为共享数据构建安装：Makefile 与 minimal-build

## 1. 本讲目标

前面十几讲里，我们把 plum 当作「个人配置管理器」来用：`rime-install` 把方案、词典拷进**当前用户的** Rime 用户目录（`rime_dir`，比如 `~/.config/ibus/rime`）。本讲换一个视角——把 Rime 数据当作**系统软件**来构建和安装，就像 `apt`、`pacman` 打包一份共享数据供所有用户使用。

学完本讲你应当能够：

1. 读懂 plum 的 `Makefile`，说出 `preset` / `all` / `minimal` / `preset-bin` / `build` / `install` / `dist` 这一组目标各自的职责与依赖关系。
2. 理解 `SRCDIR` / `OUTPUT` / `PREFIX` / `RIME_DATA_DIR` 这四个核心变量如何控制「在哪里构建、装到哪里」。
3. 说清 `rime_deployer --build` 在安装流程里扮演的「预编译」角色，以及它与「用户首次启动时编译」的区别。
4. 读懂 `scripts/minimal-build.sh` 如何用 `awk` / `sed` 裁剪 `essay.txt` 词频表、`luna_pinyin` 词典和各方案，生成一个体积更小的精简版。

## 2. 前置知识

本讲是 advanced 阶段，假设你已经掌握：

- **plum 的安装主循环**（见 u2-l3）：`install-packages.sh <target> <output_dir>` 接收一个 target（如 `:preset`）和一个输出目录，把文件拷进输出目录。
- **预设集合**（见 u2-l7）：`:preset` / `:extra` / `:all` 三档冒号集合，分别由 `preset-packages.conf` / `extra-packages.conf` / `all-packages.conf` 定义。
- **Rime 的两类数据目录**：用户目录（user data，存放个人配置、用户词典）与共享数据目录（shared data，存放方案源文件、预编译产物，供所有用户共用）。本讲中 `rime_dir` 指向用户目录，`RIME_DATA_DIR` 指向共享目录。

下面补充两个本讲要用到的外围概念。

### 2.1 什么是 Makefile

`Makefile` 是 `make` 工具的配置文件，由一组「规则」组成，每条规则的格式是：

```makefile
目标: 依赖
	命令（必须用 Tab 缩进）
```

`make 目标名` 时，会先保证「依赖」是最新的，再执行「命令」。`$@` 是 make 的自动变量，代表当前规则的目标名。本讲的 `Makefile` 几乎所有目标都标记为 `.PHONY`（伪目标），表示它们不是文件名、每次都该执行其命令。

### 2.2 什么是 rime_deployer

`rime_deployer` 是 Rime 引擎（librime）自带的一个命令行工具，用于「部署」：把人类可读的 YAML 源文件（方案、词典）编译成 Rime 运行时直接加载的二进制产物（落在 `build/` 子目录下）。Rime 在用户**首次启动**输入法时会自动做这件事，但首次编译比较慢。所以发行版打包时常常**提前**用 `rime_deployer --build` 编译好，用户开箱即用。README 也明确把 `librime>=1.3 (for rime_deployer)` 列为构建依赖。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们牵动了前面讲过的整条安装链路。

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile) | 把 Rime 数据作为系统软件构建、预编译、安装、打包的入口。定义了目标体系与四个核心变量。 |
| [scripts/minimal-build.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh) | 精简版构建脚本。只装 3 个核心包，再用 `awk`/`sed` 裁掉长尾词频、词组与部分功能，产出体积更小的数据。 |

二者都复用了 `scripts/install-packages.sh`（见 u2-l3）这台「下载 + 拷贝」机器：`Makefile` 把输出重定向到一个**暂存目录** `OUTPUT`，而不是直接写进用户的 `rime_dir`。

## 4. 核心概念与源码讲解

### 4.1 Makefile 目标体系与核心变量

#### 4.1.1 概念说明

u1-l3 里我们用 `rime-install` 把数据装进「某个用户的 Rime 目录」。但当 Linux 发行版要给 Rime 打包时，需求变了：

- 数据应当装进一个**系统级共享目录**（如 `/usr/share/rime-data`），所有用户共用，而不是装进某个人的家目录。
- 构建过程要可分步：先下载源文件到**暂存目录**，可选地预编译，再 `install` 到系统目录，最后还能打成一个**源码 tarball** 供他人复现。

`Makefile` 就是这套「发行版打包视角」的入口。它把前面讲过的 `install-packages.sh` 当作一个可复用的子程序来调用，只是把它的输出目录从 `rime_dir` 换成了 `OUTPUT`。

#### 4.1.2 核心流程

`Makefile` 的目标依赖关系如下（箭头表示「先跑依赖再跑自己」）：

```
make（默认）──► preset
preset ──► clean ──► install-packages.sh :preset  OUTPUT
extra  ──► clean ──► install-packages.sh :extra   OUTPUT
all    ──► clean ──► install-packages.sh :all     OUTPUT
minimal──► clean ──► minimal-build.sh             OUTPUT

preset-bin ──► preset ──► build ──► rime_deployer --build OUTPUT ; rm user.yaml
all-bin    ──► all    ──► build
minimal-bin──► minimal──► build

install ──► 把 OUTPUT/* 拷到 $(DESTDIR)$(RIME_DATA_DIR)
dist    ──► all ──► 打成 plum-YYYYMMDD.tar.gz
clean   ──► rm -rf OUTPUT
```

四个核心变量控制「在哪里、装到哪」：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `SRCDIR` | Makefile 自身所在目录 | plum 仓库根，用来定位 `scripts/` |
| `OUTPUT` | `$(SRCDIR)/output` | 构建暂存目录，所有源文件先落这里 |
| `PREFIX` | `/usr` | 类 Unix 标准安装前缀 |
| `RIME_DATA_DIR` | `$(PREFIX)/share/rime-data` | 系统级共享数据最终落点 |

#### 4.1.3 源码精读

先看变量定义。每个变量都用 `ifeq ($(VAR),) ... endif` 给默认值，但允许命令行覆盖：

[Makefile:1-16](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L1-L16)：定义 `SRCDIR`（用 `$(realpath $(lastword $(MAKEFILE_LIST)))` 定位 Makefile 真实路径）、`OUTPUT`（带注释提示可设成 Rime 用户目录）、`PREFIX`、`RIME_DATA_DIR`。

> 小知识：`$(lastword $(MAKEFILE_LIST))` 是当前正在处理的 Makefile 文件名，`$(realpath ...)` 把它转成绝对路径，再 `dirname` 取目录——这样无论你从哪里 `cd` 进来 `make`，`SRCDIR` 都正确指向 plum 仓库根。

接着是默认目标与三个「源文件」目标：

[Makefile:18-24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L18-L24)：`.DEFAULT_GOAL := preset` 让裸 `make` 等价于 `make preset`；`preset extra all` 三目标共用一条规则，先 `clean`，再用 `:$@` 把目标名拼成 `:preset`/`:extra`/`:all` 传给 `install-packages.sh`。

这里有两个精妙之处：

1. **`:$@` 复用预设集合**：`$@` 是 make 自动变量（当前目标名），前面加冒号就成了 u2-l7 讲过的预设集合 target。一条规则服务三个目标，零重复。
2. **第二个参数是 `$(OUTPUT)`**：对照 u2-l3，`install-packages.sh` 的签名正是 `:<configuration>|<package-name> <output-directory>`。所以 `Makefile` 只是把它的输出目录从「猜出来的 `rime_dir`」换成「构建暂存目录 `OUTPUT`」。

`minimal` 目标则走另一条路：

[Makefile:23-24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L23-L24)：`minimal` 同样先 `clean`，但调用的是 `scripts/minimal-build.sh`（见 4.3）。

最后看 `.PHONY` 声明，它把所有目标都标记为伪目标，避免与同名文件冲突：

[Makefile:71-73](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L71-L73)：列出全部伪目标。

#### 4.1.4 代码实践

**实践目标**：验证 `make` 默认目标与变量覆盖行为。

**操作步骤**：

1. 在仓库根执行 `make -n preset`（`-n` 表示 dry-run，只打印命令不执行）。
2. 再执行 `make -n OUTPUT=/tmp/rime-out all`。

**需要观察的现象**：`-n` 会打印出实际将要执行的命令序列。

**预期结果**：

- 第 1 条应看到先执行 `clean`（`rm -rf .../output`），再执行 `bash .../scripts/install-packages.sh :preset .../output`。
- 第 2 条应看到 `install-packages.sh :all /tmp/rime-out`——证明 `OUTPUT` 可在命令行覆盖，且 `$@` 被正确替换成 `all`。

> 如果你的环境没装 `make`，这条「待本地验证」；`-n` 干跑不下载任何东西，安全可试。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `preset extra all` 三目标可以共用同一条规则？`$@` 在分别执行 `make preset` 和 `make all` 时分别是什么值？

**答案**：因为它们的命令模板完全一致，只是要传给 `install-packages.sh` 的 target 不同，而这个差异正好能由自动变量 `$@`（目标名）填补。`make preset` 时 `$@` 为 `preset`，拼出 `:preset`；`make all` 时 `$@` 为 `all`，拼出 `:all`。

**练习 2**：如果不设置任何变量直接 `make`，`essay` 包的源文件最终会被拷进哪个目录？

**答案**：`$(SRCDIR)/output`（即 plum 仓库根下的 `output/` 目录）。要装到系统目录还需额外执行 `make install`。

---

### 4.2 rime_deployer 预编译与系统级安装

#### 4.2.1 概念说明

4.1 只把**源文件**下载到了 `OUTPUT`。但 Rime 运行时真正加载的是编译后的二进制。如果跳过预编译，用户第一次启用输入法时要现场编译大词典，体验很差。

因此 `Makefile` 提供了两类目标：

- **`-bin` 后缀目标**（`preset-bin` / `all-bin` / `minimal-bin`）：在对应源目标的基础上，额外跑一遍 `build`，把源文件预编译成二进制。
- **`install` 目标**：把 `OUTPUT` 里的产物（含可选的 `build/` 子目录）拷进系统级共享目录 `RIME_DATA_DIR`。

#### 4.2.2 核心流程

`-bin` 系列目标的依赖链是「先装源文件 → 再预编译」：

```
make preset-bin
   ├──► preset  （下载 :preset 源文件到 OUTPUT）
   └──► build   （rime_deployer --build OUTPUT ；删除 user.yaml）
```

`build` 做两件事：

1. `rime_deployer --build $(OUTPUT)`：把 `OUTPUT` 当作 Rime 数据目录进行部署编译，产出 `OUTPUT/build/*.bin` 等二进制。
2. `rm $(OUTPUT)/user.yaml`：删除 `user.yaml`。这个文件记录用户级状态（如上次选用的方案），属于「个人运行时状态」，不该进共享数据包，所以预编译后清掉。

`install` 则把产物分发到系统目录，规则是「顶层文件 + `build/` 子目录 + `opencc/` 子目录，存在才装」。

#### 4.2.3 源码精读

三个 `-bin` 目标结构完全相同，都是「依赖同名源目标 + 调用 build」：

[Makefile:26-33](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L26-L33)：`preset-bin: preset` 然后 `$(MAKE) build`；`all-bin`、`minimal-bin` 同理。`$(MAKE)` 而非直接写 `make`，是为了透传父进程的 `make` 路径与并行参数。

`build` 目标是预编译的核心：

[Makefile:35-37](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L35-L37)：`rime_deployer --build $(OUTPUT)` 编译，紧接着 `rm $(OUTPUT)/user.yaml` 清掉个人状态文件。

`install` 目标把产物落到系统目录，注意它对三个位置分别处理：

[Makefile:39-50](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L39-L50)：先 `install -d` 建目录，再用 `install -m 644` 拷贝 `$(OUTPUT)/*.*` 到 `$(DESTDIR)$(RIME_DATA_DIR)`；随后**条件性地**处理 `build/` 与 `opencc/` 两个子目录（`if [ -d ... ]` 才装）。

> 两个细节：
> - `$(DESTDIR)` 是「暂存根」（staged installation）惯例：发行版打包时先装到 `$(DESTDIR)$(RIME_DATA_DIR)`（一个临时根），再由包管理器整体归档，运行时并不真的写 `/usr`。本讲实践中很少用它，但它对打包者至关重要。
> - `install -m 644` 把文件权限设为 `rw-r--r--`（所有人可读、仅属主可写），符合共享只读数据的权限约定。

最后看 `dist`——打成可复现的源码 tarball：

[Makefile:55-69](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L55-L69)：`VERSION` 用当天日期；`dist` 先 `make all`，再用 `tar czf` 把整个 `plum` 目录（排除 `.git`、`output`、已有的 tarball）打包。

关键是那段注释揭示的**复现约定**：

```makefile
# To reproduce package contents of the release, set `no_update=1`:
#     tar xzf plum-YYYYMMDD.tar.gz
#     cd plum
#     no_update=1 make
#     sudo make install
```

`no_update=1` 正是 u2-l3 / u2-l5 讲过的开关：它让 `install-packages.sh` 把 `Updating package` 改成 `Found package` 且**不发网络请求**。这样解包后的 tarball 能用打包时缓存的 `package/` 目录原样复现，而不会被上游的新提交「漂移」掉版本——保证发行包内容可复现。

#### 4.2.4 代码实践

**实践目标**：从 `Makefile` 层面说清 `make preset-bin` 比 `make preset` 多做了什么。

**操作步骤**：

1. 对比 [Makefile:20-21](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L20-L21)（`preset`）与 [Makefile:26-27](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L26-L27)（`preset-bin`）两条规则。
2. 跟踪 `preset-bin` 的依赖：`preset-bin → preset → clean + install-packages.sh`，之后 `$(MAKE) build`。
3. 读 [Makefile:35-37](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L35-L37) 的 `build` 目标。

**需要观察的现象**：在脑中（或用 `make -n preset-bin`）展开 `preset-bin` 的完整命令序列。

**预期结果**：`make preset-bin` 相比 `make preset` 多出的步骤是——在下载完 `:preset` 源文件之后，额外执行：

1. `rime_deployer --build $(OUTPUT)`：预编译方案与词典为二进制（产出 `OUTPUT/build/`）。
2. `rm $(OUTPUT)/user.yaml`：清除个人状态文件。

换句话说，`make preset` 只给你源文件；`make preset-bin` 给你「源文件 + 预编译产物 + 已清理个人状态」，开箱即用。这正是 README「This saves user's time building those files on first startup」这句话的来源。

> 若本地未安装 `librime`/`rime_deployer`，`make preset-bin` 会在 `build` 步骤报 `rime_deployer: command not found`——属正常，标记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build` 目标要 `rm $(OUTPUT)/user.yaml`，却不去删别的 yaml？

**答案**：`user.yaml` 存的是用户运行时状态（上次选用的方案等），属于「个人化的、会变化的」信息，不应进入供所有人共用的共享数据。其他 `*.yaml` 是方案/词典源文件，是数据本身，必须保留。

**练习 2**：`make install` 中的 `$(DESTDIR)` 和 `$(RIME_DATA_DIR)` 分别服务谁？

**答案**：`$(DESTDIR)` 服务**打包者**——允许把安装过程重定向到一个临时暂存根，便于归档成软件包；正常运行时它为空，文件落到真正的 `$(RIME_DATA_DIR)`。`$(RIME_DATA_DIR)` 服务**最终用户**——指定共享数据的真实落点（默认 `/usr/share/rime-data`）。

---

### 4.3 minimal-build.sh 精简裁剪

#### 4.3.1 概念说明

有些场景不需要完整数据：比如嵌入式设备、移动端、只想快速体验的用户。完整 `:all` 的词典（尤其 `essay.txt` 八股文词表）体积可观。`scripts/minimal-build.sh` 就是用最小的代价造一份「够用就好」的精简版：

- 只装 3 个最核心的包：`essay`、`luna-pinyin`、`prelude`。
- 用纯文本工具（`awk` + `sed`）把装下来的文件**就地裁剪**：砍掉低频词、砍掉词组、关掉部分功能、重写 `default.yaml` 的方案列表。

注意它**没有发明新格式**，而是直接改写 Rime 既有的 YAML / 文本文件——把「生成精简数据」这件事降维成了几行 `sed`。

#### 4.3.2 核心流程

`minimal-build.sh` 分两大阶段：

```
阶段一：装源文件（复用 install-packages.sh）
   for package in essay luna-pinyin prelude:
       install-packages.sh <package> <output_dir>

阶段二：就地裁剪（在 output_dir 内）
   1. essay.txt        ── awk ──► 只留词频 ≥ 500 的条目
   2. luna_pinyin.dict ── sed ──► 版本号加 .minimal；截断到「#以下爲詞組」之前（只留单字）
   3. 每个 *.schema   ── sed ──► 版本号加 .minimal；注释掉 stroke / reverse_lookup_translator
   4. default.yaml    ── sed/grep ──► 用实际存在的方案重写 schema_list；config_version 加 .minimal
```

一个贯穿始终的约定：**所有被改动的文件，版本号都追加 `.minimal` 后缀**。这让精简版与完整版在版本字段上可区分——避免两者混淆。

#### 4.3.3 源码精读

脚本开头先定位自身目录与输出目录，然后装 3 个核心包：

[scripts/minimal-build.sh:4-9](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L4-L9)：`output_dir="$1"` 接收 Makefile 传入的 `OUTPUT`；循环对 `essay luna-pinyin prelude` 三个裸包名调用 `install-packages.sh`，第二参数仍是输出目录——与 4.1 中 `Makefile` 的调用方式完全一致。

接着 `pushd` 进入输出目录，后续所有相对路径都基于它：

[scripts/minimal-build.sh:11](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L11)：`pushd "${output_dir}"`。

**裁剪一：`essay.txt` 词频表**。这是本讲实践任务要指认的关键命令：

[scripts/minimal-build.sh:13-14](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L13-L14)：`awk '($2 >= 500) {print}' essay.txt > essay.txt.min`，再 `mv` 覆盖原文件。

> `essay.txt`（八股文）是 Rime 的共享词频表，每行形如 `词<TAB>频次`，即第一字段是词、第二字段是频次。`awk` 默认按空白分隔字段，所以 `$2` 就是频次。条件 `($2 >= 500)` 只保留高频词，砍掉频次低于 500 的长尾。若词表里高频词占比 \( p \)，则裁剪后行数近似为：
>
> \[
> N_{\text{minimal}} = p \cdot N_{\text{full}}
> \]
>
> 频次分布通常是长尾的（极少数词频次极高，大量词频次很低），所以 \( p \) 很小，体积显著下降。

**裁剪二：`luna_pinyin` 词典**。去掉词组、只留单字部分：

[scripts/minimal-build.sh:16-20](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L16-L20)：`sed -n '{ s/version: .../.minimal/ ; /^#以下爲詞組$/q ; p }'`。

拆解这段 `sed`（注意用了 `-n`，关闭自动打印，只有显式 `p` 才输出）：

1. `s/^version: \(["]*\)\([0-9.]*\)\(["]*\)$/version: \1\2.minimal\3/`：给 `version:` 行的版本号追加 `.minimal`，并兼容版本号两端可能带的引号。
2. `/^#以下爲詞組$/q`：遇到「`#以下爲詞組`」（意为「以下是词组」）这一标记行时立即退出，且因为 `-n`，该行不打印。
3. `p`：打印当前行。

效果是：保留词典头部 + 标记行**之前**的所有内容（即单字编码区），丢弃标记行及其后的词组区——明月拼音简化版词典由此瘦身。

**裁剪三：每个方案**。关掉依赖外部包或额外开销的功能：

[scripts/minimal-build.sh:22-29](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L22-L29)：对每个 `*.schema.yaml`，用 `sed` 做 3 处替换。

- `s/version: .../.minimal/`：方案版本号追加 `.minimal`。
- `s/\(- stroke\)$/#\1/`：把行尾的 `- stroke` 改成 `#- stroke`，即**注释掉笔画翻译器**（这样就不必打包 `stroke` 包）。
- `s/\(- reverse_lookup_translator\)$/#\1/`：同理注释掉反查翻译器。

> 这里用了一个稳妥的「注释」技巧：Rime 的 YAML 里，以 `#` 开头的行是注释。把 `− stroke` 前面加个 `#` 就等效于删掉这行功能，但保留了原始内容方便日后恢复。注意 `\1` 引用的是整个 `- stroke`（含前导 `- `），所以替换后是 `#- stroke`。

**裁剪四：`default.yaml` 的方案列表**。根据实际装下来的方案重写 `schema_list`：

[scripts/minimal-build.sh:31-40](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L31-L40)：先用 `ls *.schema.yaml | sed` 把每个方案文件名转成 `  - schema: <名字>` 格式，存到临时 `schema_list.yaml`；再用 `grep -Ff schema_list.yaml default.yaml` 从 `default.yaml` 里**只挑出**实际存在的方案行；最后用 `sed` 删掉 `default.yaml` 原有的 `- schema:` 行，并在 `schema_list:` 行之后用 `r` 命令读入新清单，同时给 `config_version` 加 `.minimal`。

拆解这条最复杂的 `sed`（[scripts/minimal-build.sh:34-38](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L34-L38)）：

```sed
s/^config_version: \(["]*\)\([0-9.]*\)\(["]*\)$/config_version: \1\2.minimal\3/   # 1. 版本号加后缀
/- schema:/d                                                                       # 2. 删除所有旧方案行
/^schema_list:$/r schema_list.yaml                                                 # 3. 在 schema_list: 行之后读入新清单
```

第 3 步的 `r 文件名` 是 `sed` 的「读入文件内容追加到输出流」命令，它把上一步生成的新清单原样插到 `schema_list:` 标题下方——巧妙地用 `sed` 完成了「先删后插」的列表重写。

最后 `popd` 回到原目录（[scripts/minimal-build.sh:42](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L42)）。

#### 4.3.4 代码实践

**实践目标**：精确定位「精简 `essay.txt` 词频表」的那条命令，并理解其阈值含义。

**操作步骤**：

1. 打开 [scripts/minimal-build.sh:13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L13)。
2. 读出该行：`awk '($2 >= 500) {print}' essay.txt > essay.txt.min`。
3. （可选）若本地已用 `make minimal` 跑过一次，到 `output/` 目录里执行 `wc -l essay.txt`，再对比完整版 essay 包里的同名文件行数。

**需要观察的现象**：精简版的 `essay.txt` 行数远少于完整版。

**预期结果**：

- 负责 `essay.txt` 词频表精简的命令是 [scripts/minimal-build.sh:13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L13) 的 `awk '($2 >= 500) {print}' essay.txt`。
- 它依据 `$2`（每行第二个空白分隔字段，即词频）是否 `>= 500` 来过滤，只保留高频词，丢弃低频长尾。
- 行数对比「待本地验证」（取决于 essay 包当时的实际内容），但理论上精简版应明显更小。

#### 4.3.5 小练习与答案

**练习 1**：`luna_pinyin.dict.yaml` 的 `sed` 用了 `/^#以下爲詞組$/q`。如果把 `q` 改成 `d`（删除当前行后继续），结果会有什么不同？

**答案**：`q` 是「退出整个 sed」，所以标记行**及其后所有行**都不会被处理/输出——词组区被整体丢弃。若改成 `d`，它只删除当前标记行，然后 sed 会**继续处理后面的词组行**并把它们打印出来（因为有 `p`），词组区就被保留下来——精简就失效了。所以这里必须用 `q`。

**练习 2**：为什么裁剪方案时要注释掉 `- stroke` 和 `- reverse_lookup_translator`，而不是别的功能？

**答案**：minimal 只装了 `essay`、`luna-pinyin`、`prelude` 三个包。`stroke`（笔画）需要单独的 `stroke` 包提供数据，没装就会失效报错；反查翻译器通常也要额外词典支撑。注释掉这两项让方案在「只有 minimal 三件套」的前提下仍能正常工作，避免引用缺失的数据。

**练习 3**：`grep -Ff schema_list.yaml default.yaml`（[scripts/minimal-build.sh:32](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/minimal-build.sh#L32)）里的 `-F` 起什么作用？去掉会怎样？

**答案**：`-F` 表示把 `schema_list.yaml` 里的每一行当作**固定字符串**（fixed string）而非正则来匹配。方案行形如 `  - schema: luna_pinyin`，若按正则解释，其中的 `-`、空格等是合法的（这里恰好无特殊元字符），但用 `-F` 更安全、更明确——表达「我要按字面精确匹配这一行」的意图，避免方案名里万一含有正则元字符时被误解释。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「发行版打包视角」的精简数据构建。

**任务**：在不真正安装到系统目录的前提下，干跑一遍 `minimal-bin` 的完整流程并解释每一步。

**步骤**：

1. **读依赖链**：从 [Makefile:32-33](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/Makefile#L32-L33) 出发，写出 `make minimal-bin` 的展开序列（应包含 `clean → minimal-build.sh → build`）。
2. **预测产物**：对照 4.3 的四步裁剪，预测 `OUTPUT` 目录里 `essay.txt`、`luna_pinyin.dict.yaml`、各 `*.schema.yaml`、`default.yaml` 分别会发生什么变化、版本号后缀是什么。
3. **干跑**：执行 `make -n minimal-bin`，核对打印出的命令与你手写的序列是否一致。
4. **（可选）实跑**：若本地装了 `git` 与 `rime_deployer`，执行 `make OUTPUT=/tmp/rime-min minimal-bin`，再到 `/tmp/rime-min` 里 `grep version luna_pinyin.dict.yaml`（应能看到 `.minimal` 后缀）、`wc -l essay.txt`（应明显小于完整版）。
5. **反思**：对照 `make preset-bin`（完整 + 预编译），说明 `make minimal-bin` 在「装了哪些包」和「装完后做了什么额外裁剪」两点上的区别。

**预期结果**：

- `make minimal-bin` 展开为：`clean` → `bash scripts/minimal-build.sh OUTPUT`（装 3 包 + 四步裁剪）→ `rime_deployer --build OUTPUT` → `rm OUTPUT/user.yaml`。
- 与 `make preset-bin` 相比：`minimal-bin` 装的包更少（3 个 vs 8 个），且在装完后多了一整套就地裁剪（词频过滤、词组截断、功能注释、schema_list 重写）；两者都会在最后预编译并删 `user.yaml`。
- 实跑产物「待本地验证」，取决于环境是否齐备。

## 6. 本讲小结

- `Makefile` 把 plum 从「个人配置管理器」切换到「系统软件打包器」视角：用 `OUTPUT` 作构建暂存目录，再用 `install` 把产物分发到系统级共享目录 `RIME_DATA_DIR`（默认 `/usr/share/rime-data`）。
- 四个核心变量 `SRCDIR` / `OUTPUT` / `PREFIX` / `RIME_DATA_DIR` 都有默认值且可命令行覆盖；`preset extra all` 三目标靠自动变量 `$@` 复用同一条规则，把 `:preset` 等预设集合传给 `install-packages.sh`。
- `-bin` 后缀目标比同名源目标多跑一遍 `build`：即 `rime_deployer --build $(OUTPUT)` 预编译 + `rm user.yaml` 清除个人状态，从而「开箱即用」。
- `install` 目标用 `install -d` / `install -m 644` 把顶层文件、`build/`、`opencc/` 三个位置分别拷到 `$(DESTDIR)$(RIME_DATA_DIR)`，`$(DESTDIR)` 服务打包者的暂存根需求。
- `dist` 目标打成 `plum-YYYYMMDD.tar.gz`，并约定用 `no_update=1 make` 复现——复用的正是 u2-l3/u2-l5 的「不发网络请求」开关。
- `minimal-build.sh` 只装 `essay`/`luna-pinyin`/`prelude` 三包，再用 `awk`（`essay.txt` 仅留词频 ≥ 500）与 `sed`（截断词组、注释 stroke/反查、重写 schema_list、全部版本号加 `.minimal`）就地裁剪，生成精简版数据。

## 7. 下一步学习建议

- **回顾整条链路**：本讲是 `install-packages.sh` 的又一个调用方。建议重读 u2-l3 的主循环，确认无论是 `rime-install`（用户视角）还是 `Makefile`（打包视角），底层都是同一台「下载 + 增量拷贝」机器，只是输出目录的语义不同。
- **理解 recipe 在打包中的角色**：`Makefile` 与 `minimal-build.sh` 都只搬运/裁剪源文件，不跑配方（recipe）。如果你想让某个包在打包时自动加工（如打补丁），可结合 u2-l6 的 `recipe.yaml` 机制进一步研究——但注意 `rime-install.bat` 的 ZIP 安装路径并不支持 recipe（见 u3-l2）。
- **跨平台对照**：Windows 侧没有等价的 Makefile，而是用 `rime-install.bat` 的 ZIP 安装路径覆盖类似需求（见 u3-l2）。可对比「Unix 共享数据安装」与「Windows 批处理 ZIP 安装」两套思路的差异。
- **动手延伸**：若你维护一个 Rime 发行版，可尝试写一个自定义 `my-packages.conf`（见 u2-l7），再用 `make OUTPUT=/tmp/out all` 把它装到暂存目录，体验一次完整的「打包者」流程。
