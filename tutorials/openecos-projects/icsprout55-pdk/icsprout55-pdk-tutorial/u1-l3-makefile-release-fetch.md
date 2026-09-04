# Makefile 与大文件分发机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 ICS55 要把 liberty 与 GDS 放在 GitHub Release、而不是直接提交进 git 仓库。
2. 逐行读懂 [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L1-L96) 的三大块：变量定义、下载规则、解压模式规则。
3. 手工推导 `patsubst` 把压缩包名变成解压目录路径的完整过程（例如 `ics55_LLSC_H7CH_liberty.tar.bz2` → `IP/STD_cell/.../ics55_LLSC_H7CH/liberty`）。
4. 掌握 `RELEASE_TAG`、`TOOL`、`PROXY_USE` 三个命令行参数的用法与适用场景。
5. 解释 [.gitignore](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1-L4) 的四条规则为什么恰好把「下载产物」挡在 git 之外，而又放行了 IO 库的 6 个 liberty 文件。

## 2. 前置知识

### 2.1 为什么 PDK 需要「大文件分发机制」

上一讲我们看到：这个仓库 git 只跟踪 41 个文本文件，但一个完整的 PDK 还需要两类体积庞大的数据：

- **标准单元库的 liberty 文件**：每个单元在每个工艺角（tt/ff/ss…）下都有一张时序/功耗查找表，三套库 × 多个 corner，总量可达数百 MB 的纯文本。
- **GDS 版图文件**：二进制格式，包含每个单元的完整晶体管版图多边形，体积同样可观。

如果把它们直接 `git add` 进仓库，后果是：任何人 `git clone` 都要下载全量历史中的所有大文件，仓库体积随版本迭代无限膨胀。ICS55 采用的方案是：**git 只跟踪小体积文本（LEF、CDL、verilog、cell_list、IO 库的小 liberty），大文件作为 GitHub Release 的附件（asset）存放，由一个 96 行的 Makefile 按需下载并解压到位**。这样 clone 很轻，需要完整数据时一条 `make unzip` 补齐。

> 术语：**GitHub Release** 是仓库的「发布版」页面，可以挂任意附件供下载；**asset** 指发布版里的每个附件文件。

### 2.2 make 的最小知识

本讲会用到的 make 概念，先用一句话版本预热，后面结合源码细讲：

| 概念 | 一句话解释 |
|---|---|
| 变量赋值 `?=` | 只在该变量尚未定义时才赋值（因此命令行 `make X=值` 可以覆盖它） |
| 变量赋值 `:=` | 立即展开赋值，右边的函数在定义时就求值 |
| `$(patsubst 原模式,替换模式,文本)` | 对「文本」里每个空格分隔的单词做模式替换，`%` 是通配符 |
| 模式规则（pattern rule） | 目标里含 `%` 的规则，一次编写即可匹配一族目标 |
| 自动变量 `$@` `$<` `$*` | 分别代表「当前目标」「第一个先决条件」「`%` 匹配到的那段字符串」 |

### 2.3 三个相关工具

- **curl**：命令行 HTTP 客户端，本讲中既用来查询 GitHub API（`-s` 静默模式），也用来下载文件（`-fL`：失败返回错误码、跟随重定向）。
- **wget**：另一款常用下载工具，是 Makefile 里 `TOOL=wget` 的备选项。
- **tar + bzip2**：`tar -xjvf 包名.tar.bz2` 解压 bzip2 压缩的归档，`-j` 选项要求系统已安装 `bzip2`——这正是 [README.md:L12](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L12) 提醒「请先安装 bzip2」的原因。

## 3. 本讲源码地图

| 文件 | 行数 | 在本讲中的角色 |
|---|---|---|
| [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L1-L96) | 96 | 主角：定义要下载的 7 个压缩包、下载脚本、解压模式规则与清理目标 |
| [.gitignore](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1-L4) | 4 | 决定「下载产物不进 git」的边界 |
| [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L10-L35) | — | Usage 一节是 Makefile 的「用户手册」，给出四种调用方式 |

回顾上一讲的目录结构（完整树见 [README.md:L67-L106](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L67-L106)）：三套标准单元库 `ics55_LLSC_H7CH/H7CL/H7CR` 与 IO 库 `ICsprout_55LLULP1233_IO_251013` 各有一个 `liberty` 和 `gds` 视图目录。本讲的 Makefile 所做的一切，就是把 Release 上的压缩包精确「灌」进这些目录。

## 4. 核心概念与源码讲解

### 4.1 Release 资产清单：要下载什么

#### 4.1.1 概念说明

下载机制的第一步是**声明式地列出所有需要的文件**。Makefile 用一组变量把「7 个压缩包」组织成清晰的层级：先按内容类型（liberty / GDS）分，再在 GDS 内按库类型（标准单元 / IO）分，最后汇总成一个总表。这种「变量套变量」的写法让新增一个库时只需要在一处追加一行文件名。

#### 4.1.2 核心流程

```text
RELEASE_FILE_LIB（3 个 liberty 包）
RELEASE_FILE_GDS_STD（3 个标准单元 gds 包）──┐
RELEASE_FILE_GDS_IO（1 个 IO gds 包）────────┴→ RELEASE_FILE_GDS（4 个）
                                                ↓
                    RELEASE_FILE = 3 + 4 = 7 个压缩包（即下载总清单）
```

#### 4.1.3 源码精读

**仓库身份与三个开关变量**（[Makefile:L4-L10](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L4-L10)）定义了 GitHub 上游仓库的 org / repo 名，以及代理地址、代理开关和版本标签：

```makefile
ORGS_NAME := openecos-projects
REPO_NAME := icsprout55-pdk

PROXY_URL ?= https://gh-proxy.org/
PROXY_USE ?= false

RELEASE_TAG ?= latest
```

注意这里 `:=` 与 `?=` 的区别：仓库身份是固定事实用 `:=`；而三个开关用 `?=`，意味着你可以在命令行覆盖，例如 `make unzip PROXY_USE=true`。

**压缩包清单**（[Makefile:L11-L20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L20)）：

```makefile
RELEASE_FILE_LIB := ics55_LLSC_H7CH_liberty.tar.bz2 \
                    ics55_LLSC_H7CL_liberty.tar.bz2 \
                    ics55_LLSC_H7CR_liberty.tar.bz2

RELEASE_FILE_GDS_STD := ics55_LLSC_H7CH_gds.tar.bz2 \
                        ics55_LLSC_H7CL_gds.tar.bz2 \
                        ics55_LLSC_H7CR_gds.tar.bz2
RELEASE_FILE_GDS_IO := ICsprout_55LLULP1233_IO_251013_gds.tar.bz2
RELEASE_FILE_GDS    := $(RELEASE_FILE_GDS_STD) $(RELEASE_FILE_GDS_IO)
RELEASE_FILE        := $(RELEASE_FILE_LIB) $(RELEASE_FILE_GDS)
```

这段代码做的事情：**H7CH/H7CL/H7CR 三套库各有一个 liberty 包和一个 gds 包，IO 库只有一个 gds 包**（IO 的 6 个小体积 liberty 已直接放在 git 里，上一讲确认过），合计 7 个 `.tar.bz2`。行末的 `\` 是 make 的续行符，让清单保持可读的缩进。

一个值得体会的命名约定：**压缩包的文件名不是随便起的，它编码了「这是哪个库的哪种视图」**——`ics55_LLSC_H7CH` + `_liberty` + `.tar.bz2`。下一节将看到，解压目标路径正是用 `patsubst` 从文件名里把库名「抽」出来再拼回路径的，文件名就是这个推导协议的数据源。

#### 4.1.4 代码实践

**实践：用 make 自己的求值能力验证清单**

1. 实践目标：不下载任何东西，确认 `RELEASE_FILE` 与 `DECOMP_DIR` 两个变量的展开结果。
2. 操作步骤：在仓库根目录执行（`-p` 让 make 打印数据库后退出，`--no-print-directory` 去掉目录提示）：

   ```bash
   make -p --no-print-directory 2>/dev/null | grep -E '^(RELEASE_FILE|DECOMP_DIR) '
   ```

3. 需要观察的现象：`RELEASE_FILE` 一行应包含 7 个 `.tar.bz2` 文件名；`DECOMP_DIR` 一行应包含 7 个目录路径。
4. 预期结果：7 个路径与本讲 4.3.2 节推导的表格逐条一致（该输出由源码推导，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果 ICsprout 未来发布了 RAM 库的 liberty 包（假设名为 `ics55_RAM_liberty.tar.bz2`），按现有结构应在哪个变量里追加？还需要改动哪几处？

<details>
<summary>参考答案</summary>

在 `RELEASE_FILE_LIB`（[Makefile:L11-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L13)）里追加文件名。但由于该库解压后的目录前缀不同（RAM 库大概率不在 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100` 下），`DECOMP_DIR_LIB` 的 `patsubst` 模式与模式规则 `$(DECOMP_DIR_LIB_P)/%/liberty` 也需要相应调整，例如新增一个独立的 `DECOMP_DIR_*_P` 前缀和一条新的模式规则（参照 IO gds 的做法：前缀不同就单开一条规则）。
</details>

**练习 2**：`PROXY_USE` 和 `RELEASE_TAG` 为什么用 `?=` 而不是 `:=` 赋值？

<details>
<summary>参考答案</summary>

`?=` 只在变量未定义时赋默认值。这样命令行上的 `make unzip RELEASE_TAG=v1.10.100` 会先定义 `RELEASE_TAG`，Makefile 里的 `?= latest` 就不会覆盖它；若写成 `:=`，Makefile 内的赋值无条件生效（实际上命令行变量优先级仍高于 `:=`，但 `?=` 的语义在这里更清晰地表达了「默认值、可被覆盖」的意图，也允许通过 `make` 环境变量预设）。
</details>

---

### 4.2 下载规则：从 GitHub API 到本地压缩包

#### 4.2.1 概念说明

7 个压缩包在本地并不存在，make 把它们视为**需要构建的文件目标**。这套设计的巧妙之处在于：**「下载」在 make 眼里和「编译」是同一种事情**——目标文件不存在（或先决条件更新）就执行 recipe 生成它。因此不需要写专门的「下载流程脚本」，一条模式统一的规则就够了。

#### 4.2.2 核心流程

对每一个压缩包，recipe 执行以下流水线：

```text
① 根据 RELEASE_TAG 选择 GitHub API 端点
     latest  → https://api.github.com/repos/<org>/<repo>/releases/latest
     指定tag → https://api.github.com/repos/<org>/<repo>/releases/tags/<tag>
② curl 查询该 Release 的 JSON 元数据
③ grep 提取包含目标文件名的 browser_download_url 行
④ cut -d '"' -f 4 从 JSON 行里切出纯 URL
⑤ URL 为空 → 打印错误并 exit 1（fail-fast）
⑥ PROXY_USE=true → 在 URL 前拼上代理前缀
⑦ TOOL=wget ? wget : curl -fL 下载到 <文件名>.part
⑧ 下载失败 → 删 .part 并退出；成功 → mv 成正式文件名
```

#### 4.2.3 源码精读

**规则头**（[Makefile:L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L34)）：`$(RELEASE_FILE):` 是多目标规则，make 会把它当作 7 条独立规则——每个压缩包一个目标，共用同一份 recipe。

**第一步：选择 API 端点**（[Makefile:L36-L40](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L36-L40)）：

```makefile
@if [ "$(RELEASE_TAG)" = "latest" ]; then \
    API_PATH="releases/latest"; \
else \
    API_PATH="releases/tags/$(RELEASE_TAG)"; \
fi; \
```

这段是 shell 的 if/else（整条 recipe 被 `\` 连成一个 shell 进程）。`$(RELEASE_TAG)` 由 make 展开，`API_PATH` 前的 `$$` 转义成 shell 变量的 `$`。这个版本固定能力是 2026 年 7 月的提交 `9a4df8c`（"feat: allow pinning release version in Makefile"）加入的——在此之前只能永远拉 latest，无法复现旧版本数据。

**第二步与第三步：查询并抽取下载 URL**（[Makefile:L41-L43](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L41-L43)）：

```makefile
RELEASE_URL=$$(curl -s "https://api.github.com/repos/$(ORGS_NAME)/$(REPO_NAME)/$$API_PATH" | \
    grep -E "browser_download_url.*$(@)" | \
    cut -d '"' -f 4); \
```

这是本规则最精彩的一行，三节管道逐段拆开看：

- `curl -s <API 地址>`：拿到 Release 元数据的 JSON，其中每个 asset 都有一个 `"browser_download_url": "https://..."` 字段。
- `grep -E "browser_download_url.*$(@)"`：`$(@)` 是自动变量「当前目标」，即正在构建的压缩包文件名。用它过滤出**属于这个目标的那一行**——这就是文件名必须唯一编码库身份的原因。
- `cut -d '"' -f 4`：以双引号为分隔符取第 4 个字段。JSON 行形如 `"browser_download_url": "https://…/xxx.tar.bz2",`，按 `"` 切开后：第 2 段是字段名、第 4 段恰好是完整 URL。

**第四步：fail-fast 校验**（[Makefile:L44-L49](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L44-L49)）：如果 `RELEASE_URL` 为空（Release 里没有这个资产），打印错误信息并列出全部期望文件名后 `exit 1`，绝不带病继续。这来自提交 `fb3af20`（"fix: add fail-fast to download target"）——否则缺文件的包会让后续解压步骤报出更难定位的 tar 错误。

**第五步：代理拼接与双工具下载**（[Makefile:L50-L59](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L50-L59)）：

```makefile
if [ "$(PROXY_USE)" = "true" ]; then \
    RELEASE_URL="$(PROXY_URL)$$RELEASE_URL"; \
fi; \
if [ "$(TOOL)" = "wget" ]; then \
    wget -O "$(@).part" "$$RELEASE_URL"; \
else \
    curl -fL -o "$(@).part" "$$RELEASE_URL"; \
fi || { rm -f "$(@).part"; exit 1; }; \
mv "$(@).part" "$(@)"; \
```

三个工程细节值得学习：

1. **代理是「URL 前缀」方案**：`https://gh-proxy.org/` 直接拼在原始 URL 前面，就变成了走代理的镜像地址，不需要额外的代理环境变量（该能力由提交 `86233b1` 引入，配合 README 中的说明）。
2. **先写 `.part` 再改名**：下载目标带 `.part` 后缀，成功后才 `mv` 成正式文件名。中途断网只会留下半截的 `.part`（且失败分支立刻删掉它），不会留下一个「看起来完整、实际损坏」的压缩包骗过下次 make 的时间戳判断。
3. **`TOOL` 没有默认值定义**：整个 Makefile 找不到 `TOOL :=` 或 `TOOL ?=`，它只出现在 `if [ "$(TOOL)" = "wget" ]` 里。未定义变量展开为空字符串，不等于 `"wget"`，于是走 else 分支——**默认工具是 curl**，这与 [README.md:L21](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L21) 的说明一致。

#### 4.2.4 代码实践

**实践：干跑观察下载脚本的真面目**

1. 实践目标：不真正下载，看清 make 会为每个压缩包执行什么命令。
2. 操作步骤：

   ```bash
   make -n unzip
   ```

   `-n`（dry-run）只打印命令不执行。输出较长，可以配合管道过滤：

   ```bash
   make -n unzip | grep -c 'browser_download_url'   # 统计下载脚本出现的次数
   make -n unzip | grep -E 'tar -xjvf'              # 只看解压命令
   ```

3. 需要观察的现象：`browser_download_url` 那行 shell 管道应出现 **7 次**（每个压缩包一次）；`tar -xjvf` 也应出现 7 次。
4. 预期结果：命令序列的大致形态为「`[unzip] start...` → clean-dir 的两条 `find` → 7 组【下载脚本 + mkdir/tar/touch 三连】→ clean-bz2 的 `find`」。由于 recipe 内多条命令用 `\` 连成一行 shell，`make -n` 会把这些长行原样打印。此输出由源码推导，**待本地验证**。
5. 再试一次带参数的干跑，观察差异：

   ```bash
   make -n unzip RELEASE_TAG=v1.10.100
   ```

   预期打印中出现 `API_PATH="releases/tags/v1.10.100"` 而不是 `API_PATH="releases/latest"`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `grep` 的正则 `browser_download_url.*$(@)` 不会误匹配到别的压缩包？举例说明。

<details>
<summary>参考答案</summary>

因为 7 个文件名互不为彼此的子串。例如目标是 `ics55_LLSC_H7CH_liberty.tar.bz2` 时，H7CL 资产的 URL 行是 `...ics55_LLSC_H7CL_liberty.tar.bz2`，其中不包含字符串 `ics55_LLSC_H7CH_liberty.tar.bz2`，grep 匹配失败被过滤掉；唯一包含该完整文件名的只有 H7CH 自己那一行。
</details>

**练习 2**：如果把 `curl -s` 改成 `curl -sL`（跟随重定向），对哪一步有影响？`-s` 和解压命令里 `tar` 的 `-j` 分别去掉会发生什么？

<details>
<summary>参考答案</summary>

GitHub 的 `api.github.com` 元数据接口通常直接返回 JSON，`-s` 只是把进度条静音，去掉不影响功能只影响观感。下载那一处已经用了 `-fL`（`-f` 让 HTTP 错误返回失败退出码、`-L` 跟随 Release 资产的重定向），这个 `-L` 是必需的——GitHub 资产 URL 会 302 到实际的存储地址。`tar` 的 `-j` 指定 bzip2 解压，去掉后 tar 无法识别 `.tar.bz2` 格式会直接报错，这正是 README 要求预装 bzip2 的原因。
</details>

---

### 4.3 patsubst 与模式规则：压缩包如何落到库目录

#### 4.3.1 概念说明

现在本地有了压缩包，下一个问题是：**它们各自应该解压到哪个目录？** 最朴素的写法是手写 7 条规则，但维护者选择了更聪明的方案：

- 用 `$(patsubst ...)` 从「压缩包文件名」**批量推导**出「解压目标目录」；
- 用**模式规则**（pattern rule）为这一族目标编写**一条**解压配方。

两者共享同一个关键道具——`%` 通配符：在 `patsubst` 里它捕获文件名中的库名，在模式规则里它把目标目录和先决条件压缩包「锁」在一起。理解了 `%` 的这两次出场，这一节的机制就全通了。

#### 4.3.2 核心流程

`patsubst 原模式, 替换模式, 文本` 对文本中每个以空格分隔的单词独立匹配：单词匹配原模式时，`%` 捕获的子串会被原样填进替换模式的 `%` 里。

**liberty 的推导**（[Makefile:L22-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22-L23)）：

```makefile
DECOMP_DIR_LIB_P := IP/STD_cell/ics55_LLSC_H7C_V1p10C100
DECOMP_DIR_LIB   := $(patsubst %_liberty.tar.bz2, $(DECOMP_DIR_LIB_P)/%/liberty, $(RELEASE_FILE_LIB))
```

变量名末尾的 `_P` 取自 Prefix（前缀）——它就是替换模式里的路径前缀。三个压缩包的展开结果：

| 输入单词（压缩包名） | `%` 捕获 | 输出（解压目标目录） |
|---|---|---|
| `ics55_LLSC_H7CH_liberty.tar.bz2` | `ics55_LLSC_H7CH` | `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty` |
| `ics55_LLSC_H7CL_liberty.tar.bz2` | `ics55_LLSC_H7CL` | `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/liberty` |
| `ics55_LLSC_H7CR_liberty.tar.bz2` | `ics55_LLSC_H7CR` | `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty` |

**gds 的推导**（[Makefile:L25-L28](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L25-L28)）：

```makefile
DECOMP_DIR_GDS_STD_P := IP/STD_cell/ics55_LLSC_H7C_V1p10C100
DECOMP_DIR_GDS_IO_P  := IP/IO
DECOMP_DIR_GDS       := $(patsubst %_gds.tar.bz2, $(DECOMP_DIR_GDS_STD_P)/%/gds, $(RELEASE_FILE_GDS_STD)) \
                        $(patsubst %_gds.tar.bz2, $(DECOMP_DIR_GDS_IO_P)/%/gds, $(RELEASE_FILE_GDS_IO))
```

这里出现了一个关键设计：**gds 需要两条 patsubst**，因为标准单元和 IO 的解压根前缀不同（`IP/STD_cell/ics55_LLSC_H7C_V1p10C100` vs `IP/IO`），而 `patsubst` 一次只能用一个替换前缀。IO 包的推导：

| 输入单词 | `%` 捕获 | 输出 |
|---|---|---|
| `ICsprout_55LLULP1233_IO_251013_gds.tar.bz2` | `ICsprout_55LLULP1233_IO_251013` | `IP/IO/ICsprout_55LLULP1233_IO_251013/gds` |

最终汇总（[Makefile:L30](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L30)）：`DECOMP_DIR := $(DECOMP_DIR_LIB) $(DECOMP_DIR_GDS)`，共 7 个目录——它们就是 make 眼中「要构建的产物」。

#### 4.3.3 源码精读

**三条解压模式规则**（[Makefile:L62-L78](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L78)）。以 liberty 为例：

```makefile
$(DECOMP_DIR_LIB_P)/%/liberty: %_liberty.tar.bz2
	@echo "\n[unzip] decompressing: $< -> $(DECOMP_DIR_LIB_P)/$*/"
	@mkdir -p $@
	@tar -xjvf $< -C $(DECOMP_DIR_LIB_P)/$*/
	@touch $@
```

规则展开后目标模式是 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/%/liberty`。当 make 要构建 `.../ics55_LLSC_H7CH/liberty` 时，`%` 匹配到 `ics55_LLSC_H7CH`，于是先决条件自动变成 `ics55_LLSC_H7CH_liberty.tar.bz2`——**目录和压缩包通过 `%` 绑定，同一条规则覆盖三个库**。gds 有两条规则（[Makefile:L68-L72](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L68-L72) 标准单元、[Makefile:L74-L78](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L74-L78) IO），原因同样是路径前缀不同。

recipe 里的自动变量分工：

| 自动变量 | 在此代表 | 例 |
|---|---|---|
| `$@` | 目标（解压目录） | `IP/.../ics55_LLSC_H7CH/liberty` |
| `$<` | 第一个先决条件（压缩包） | `ics55_LLSC_H7CH_liberty.tar.bz2` |
| `$*` | `%` 匹配的库名 | `ics55_LLSC_H7CH` |

四条命令各自的作用：`mkdir -p $@` 建目录；`tar -xjvf $< -C 前缀/$*/` 把包解压到**库根目录**（注意 `-C` 的落点是 `$*/` 而不是 `$@`，说明压缩包内部的路径结构决定文件最终位置——包内是否自带 `liberty/` 顶层目录需下载后确认，**待本地验证**）；`touch $@` 显式刷新目录时间戳，确保目标时刻新于压缩包，下次 make 不会因时间戳过旧而重复解压；开头的 `echo` 打印人类可读的进度。

**总装目标 unzip**（[Makefile:L80-L84](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L80-L84)）：

```makefile
unzip: start clean-dir $(DECOMP_DIR) clean-bz2
	@echo "\n[unzip] done!"

start:
	@echo "[unzip] start..."
```

`unzip` 的先决条件按书写顺序串起了完整流程（make 默认串行、按序构建先决条件）：

```text
start      → 打印开始信息（.PHONY 目标，[Makefile:L83-L84](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L83-L84)）
clean-dir  → 先删掉旧的 liberty/gds 目录（.PHONY）
$(DECOMP_DIR) → 7 个目录目标：
                 目录不存在 → 找到模式规则 → 先决条件 tar 包不存在
                            → 触发 4.2 节下载规则 → 解压 → touch
clean-bz2  → 删除全部 *.tar.bz2（.PHONY）
```

两个容易忽略的要点：

1. **`clean-dir` 故意排在 `$(DECOMP_DIR)` 前面**：目录被 touch 过之后时间戳新于压缩包，直接重跑 `make unzip` 会被 make 判定「已是最新」而跳过解压；先删目录保证了每次 `make unzip` 都是幂等的全新解压。
2. **`download` 目标（[Makefile:L86](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L86)）只是 `$(RELEASE_FILE)` 的别名**：`make download` 只下载 7 个包不解压，方便你先验货；而 `make unzip` 通过目录→压缩包的依赖链**隐含**了下载，无需显式写 `download`。

**两个清理目标**（[Makefile:L88-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L88-L95)）：

```makefile
clean-bz2:
	@find ./ -name "*.tar.bz2" -exec rm -fv {} \; || true

clean-dir:
	@find IP/STD_cell -depth -type d -name "liberty" -exec rm -rfv {} \; || true
	@find IP -depth -type d -name "gds" -exec rm -rfv {} \; || true
```

`clean-bz2` 清压缩包，`clean-dir` 清解压目录。注意 `clean-dir` 的两行 find 用 `-depth`（深度优先遍历）配合 `rm -rf`，避免 find 遍历过程中目录先于其子目录被删除而报警；结尾 `|| true` 让「没找到可删的东西」不构成失败。另外注意范围：liberty 只清 `IP/STD_cell` 下的，gds 则清 `IP` 下全部（含 IO 的 gds）——与 `.gitignore` 的覆盖范围一一呼应。

#### 4.3.4 代码实践

**实践：验证 `%` 的绑定关系**

1. 实践目标：亲手向 make 请求一个具体的解压目录，验证模式规则的匹配与先决条件推导。
2. 操作步骤（`-n` 干跑，不会真的下载）：

   ```bash
   make -n "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty"
   ```

3. 需要观察的现象：make 输出中应先出现针对 `ics55_LLSC_H7CH_liberty.tar.bz2` 的下载脚本，随后是 `mkdir -p`、`tar -xjvf ics55_LLSC_H7CH_liberty.tar.bz2 -C IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/` 与 `touch` 三条命令。
4. 预期结果：请求的目录路径中，除库名 `ics55_LLSC_H7CH` 之外的部分都来自规则里的字面前缀；`$*` 恰好等于库名。若把路径中的库名换成 `ics55_LLSC_H7CL`，压缩包名应随之变为 `ics55_LLSC_H7CL_liberty.tar.bz2`（待本地验证）。
5. 若 make 报 `No rule to make target`，检查路径是否与 [Makefile:L22](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22) 的前缀逐字符一致——模式规则的 `%` 只接管库名那一段。

#### 4.3.5 小练习与答案

**练习 1**：`make download` 和 `make unzip` 的区别是什么？为什么 `unzip` 的依赖里没有出现 `download`？

<details>
<summary>参考答案</summary>

`download`（[Makefile:L86](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L86)）只依赖 7 个压缩包文件，执行后仓库里留下 `.tar.bz2`；`unzip` 依赖 7 个解压目录，而目录的先决条件正是压缩包，所以下载作为传递依赖被自动触发，最后 `clean-bz2` 还会把包删掉。这体现了 make 依赖图的优势：不需要写显式的「步骤 1 再步骤 2」脚本，目标之间的先决关系自然形成了执行顺序。
</details>

**练习 2**：假如你把 `clean-dir` 从 `unzip` 的依赖列表中移到 `$(DECOMP_DIR)` 之后，第二次运行 `make unzip` 会发生什么？

<details>
<summary>参考答案</summary>

第二次运行时，7 个目录的时间戳已被上一次的 `touch` 刷新、新于（已被删除的）压缩包，make 会判定目录「已是最新」而跳过解压；随后 `clean-dir` 反而把刚生成（或上次遗留）的目录删掉——结果是你得到一个空仓库。原顺序「先清理、后解压」正是为了避免这种自毁式的执行序。
</details>

**练习 3**：为什么标准单元 gds 和 IO gds 必须写成两条模式规则，而三套标准单元库的 liberty 一条规则就够？

<details>
<summary>参考答案</summary>

模式规则的字面前缀必须与目标路径逐段一致。三套标准单元库的 liberty 目录共享同一个父目录 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100`，`%` 只需接管各不相同的库名段，一条规则即可；而 gds 的目的地分属 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/.../gds` 和 `IP/IO/.../gds` 两个不同前缀，一个 `%` 无法同时表达两种前缀，只能按前缀拆成两条规则（这与 `patsubst` 需要两次调用是同一个原因）。
</details>

---

### 4.4 三个可调参数与 .gitignore 的边界

#### 4.4.1 概念说明

Makefile 对外暴露三个（加一个 `PROXY_URL` 共四个）命令行参数，分别解决三类现实问题；而 `.gitignore` 的四行规则则回答另一个问题：**下载和解压产生的文件，为什么一个都不该进 git？** 参数是「怎么用」，`.gitignore` 是「用完之后仓库保持什么样子」，两者合成这套分发机制的对外接口。

#### 4.4.2 核心流程

| 参数 | 默认值 | 生效位置 | 解决的问题 | 用法示例 |
|---|---|---|---|---|
| `RELEASE_TAG` | `latest` | [Makefile:L36-L40](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L36-L40) | 版本可复现：固定拉某个 Release | `make unzip RELEASE_TAG=v1.10.100` |
| `TOOL` | （未定义，实际为 curl） | [Makefile:L54-L58](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L54-L58) | 环境里没有 curl 或 curl 行为异常时换 wget | `make unzip TOOL=wget` |
| `PROXY_USE` | `false` | [Makefile:L51-L53](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L51-L53) | 直连 GitHub 慢时走镜像前缀加速 | `make unzip PROXY_USE=true` |
| `PROXY_URL` | `https://gh-proxy.org/` | 同上 | 换用其他代理镜像 | `make unzip PROXY_USE=true PROXY_URL=https://别的镜像/` |

三个参数可自由组合，[README.md:L21-L35](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L21-L35) 给出的 `make unzip PROXY_USE=true TOOL=wget` 就是最全的组合。

#### 4.4.3 源码精读

**.gitignore 的四行**（[.gitignore:L1-L4](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1-L4)）：

```gitignore
/**/STD_cell/**/liberty/
/**/gds/
*.tar.bz2
*.mk
```

逐行解释（`**` 在 gitignore 语义中匹配任意层级的目录）：

1. `/**/STD_cell/**/liberty/` —— 忽略标准单元库下**任意深度**的 `liberty` 目录，即 `make unzip` 解压出的三套库 liberty。注意路径被明确限定在 `STD_cell` 之内，**所以 IO 库的 `IP/IO/.../liberty/` 不受影响**——这就是上一讲「git 里能看到 6 个 IO liberty 文件」的机制根源：它们体积小、留在 git 内，而标准单元 liberty 体积大、走 Release 下载。
2. `/**/gds/` —— 忽略**任何位置**的 `gds` 目录，包括标准单元与 IO 的全部版图。GDS 是二进制大文件，一律不进 git。
3. `*.tar.bz2` —— 下载过程中落在仓库根目录的 7 个压缩包（`clean-bz2` 会删，但下载中途中断时可能残留），一律不进 git。
4. `*.mk` —— 忽略任意 `.mk` 文件。注意主文件名为 `Makefile`（无后缀），不受影响；这条规则针对的是用户或工具产生的 make 片段文件（例如自己写的 `include` 片段）。它由 HEAD 提交 `68d89ed`（"docs: add *.mk rules"）刚刚加入，提交信息未详述动机，具体场景**待确认**。

把这四行和 Makefile 放在一起看，能读出一层设计意图：**`make unzip` 在仓库里产生的每一类产物（压缩包、标准单元 liberty、全部 gds），都恰好有一条 gitignore 规则接住**。所以无论下载是否成功、解压是否中断，`git status` 始终保持干净——大文件分发完全「透明」于版本控制。

#### 4.4.4 代码实践

**实践一：让 git 亲口告诉你它忽略了什么**

1. 实践目标：用 `git check-ignore -v` 把抽象的 gitignore 规则落到具体路径上。
2. 操作步骤（在仓库根目录执行，路径无需真实存在）：

   ```bash
   git check-ignore -v \
     IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/foo.lib \
     IP/IO/ICsprout_55LLULP1233_IO_251013/gds/xxx.gds \
     IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib \
     ics55_LLSC_H7CH_liberty.tar.bz2 \
     my-notes.mk
   ```

3. 需要观察的现象：前两条与最后一条路径会输出「命中的规则文件:行号 + 规则内容 + 路径」；第三条（IO 库已跟踪的 liberty）**不会**出现在输出里。
4. 预期结果：`foo.lib` 命中 `.gitignore:1` 的 `/**/STD_cell/**/liberty/`；`xxx.gds` 命中第 2 行 `/**/gds/`；`*.tar.bz2` 命中第 3 行；`my-notes.mk` 命中第 4 行；IO liberty 无命中——因为它不在 `STD_cell` 路径下（`git check-ignore` 对**已被跟踪**的文件本就不生效，此处以路径模式判断即可，待本地验证）。

**实践二（需网络）：完整走一遍带版本固定的下载**

1. 实践目标：验证 `RELEASE_TAG` 固定版本后，liberty/gds 目录被正确创建且不出现在 git status 中。
2. 操作步骤：

   ```bash
   make unzip RELEASE_TAG=v1.10.100    # 或默认 make unzip 拉 latest
   git status --short
   ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty | head
   ```

3. 需要观察的现象：下载日志逐包打印 `[download] getting ...` 与 `[unzip] decompressing: ...`；结束后仓库根目录**没有**残留 `.tar.bz2`（已被 `clean-bz2` 删除）。
4. 预期结果：三套标准单元库的 `liberty/` 与四个 `gds/` 目录出现真实文件（内部具体文件名**待本地验证**）；`git status --short` 输出为空——所有新目录都被 `.gitignore` 前两行吸收。若网络不通，改试 `make unzip PROXY_USE=true` 或 `TOOL=wget`。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 gitignore 的第一条写成更简单的 `/**/liberty/`？

<details>
<summary>参考答案</summary>

那样会把 IO 库的 `liberty/` 目录也忽略掉。但 IO 库的 6 个 liberty 文件（每个约千余行的小文件）是**有意保留在 git 内**的：clone 后无需任何下载就能做 IO 相关的时序实验。写成 `/**/STD_cell/**/liberty/` 精确地把忽略范围限定在「体积大、需要下载」的标准单元 liberty 上，这是一行 gitignore 里体现的「按体积划界」策略。
</details>

**练习 2**：你的同事说「我 `make unzip` 之后 `git status` 干干净净，说明 make 什么都没做」。如何用两条命令反驳或证实？

<details>
<summary>参考答案</summary>

`git status` 干净恰恰是 `.gitignore` 设计的结果，不能证明 make 没干活。用文件系统直接验证：`ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty`（目录里出现了解压出的 liberty 文件）和 `git check-ignore -v IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty`（输出显示该路径命中 `.gitignore:1` 的规则）——前者证明产物存在，后者解释了为什么 git 对它视而不见。
</details>

**练习 3**：`make unzip RELEASE_TAG=v1.10.100` 与 `make unzip` 在 GitHub API 调用上的唯一差别是什么？

<details>
<summary>参考答案</summary>

只是 API 端点不同：默认 `latest` 时请求 `https://api.github.com/repos/openecos-projects/icsprout55-pdk/releases/latest`，固定版本时请求 `.../releases/tags/v1.10.100`（见 [Makefile:L36-L40](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L36-L40)）。后续的 grep 提取、代理、下载、解压逻辑完全复用。固定版本的意义在于可复现：PDK 迭代后，你的实验环境仍能拉到当初那份数据。
</details>

## 5. 综合实践

**任务：为 ICS55 写一份「分发机制体检报告」**

不下载任何大文件（全程使用 `make -n`、`make -p`、`git check-ignore` 等只读手段），产出一份包含以下四节的报告：

1. **资产清单节**：从 `make -p` 的输出中摘出 `RELEASE_FILE` 与 `DECOMP_DIR` 的完整展开值，画出 7 条「压缩包 → 解压目录」的映射表，并标注每条映射命中的是哪一条模式规则（[Makefile:L62](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62)、[L68](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L68) 或 [L74](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L74)）。
2. **执行序节**：用 `make -n unzip`、`make -n unzip RELEASE_TAG=v1.10.100`、`make -n unzip TOOL=wget PROXY_USE=true` 三次干跑，记录三份输出中 API 端点字符串、下载命令（`curl -fL` 还是 `wget -O`）与 URL 前缀（是否出现 `https://gh-proxy.org/`）的差异，整理成参数 × 效果对照表。
3. **边界节**：用 `git check-ignore -v` 检验 5 类路径——标准单元 liberty、IO liberty、标准单元 gds、IO gds、`.tar.bz2`——记录哪些被忽略、命中第几行规则，并特别说明 IO liberty 为何幸免。
4. **反证节**：回答一个问题——如果把 `.gitignore` 第一条改成 `/**/liberty/`，`git status` 会出现什么变化？（提示：对**已被跟踪**的文件，gitignore 并不会让 git 停止跟踪；这条规则的真正作用是挡住**新增/未跟踪**的文件。结合 `git ls-files | grep liberty` 的输出说明你的推理。）

完成后你应当能用一句话向别人讲清这套机制：「git 存小文件，Release 存大文件，`make unzip` 用 7 个文件名同时驱动下载与解压，`.gitignore` 保证整个过程对 git 完全透明。」

## 6. 本讲小结

- ICS55 用 **git（41 个文本文件）+ GitHub Release（7 个压缩包）** 的两层结构解决 PDK 大文件问题：clone 轻、按需补齐。
- 下载规则的核心是一条三节管道：`curl -s` 查 Release 元数据 → `grep browser_download_url.*$(@)` 选中当前目标 → `cut -d '"' -f 4` 切出 URL；配合 `.part` 临时文件与 fail-fast，保证不留损坏包、缺资产立刻报错。
- **文件名即协议**：`patsubst %_liberty.tar.bz2, 前缀/%/liberty, ...` 从压缩包名抽出库名再拼回路径，一次调用推导出全部解压目标；模式规则用同一个 `%` 把「目录目标」与「压缩包先决条件」绑定，三条规则覆盖 7 个目标。
- `unzip` 目标通过 `start → clean-dir → 7 个目录 → clean-bz2` 的先决条件顺序实现幂等重跑；`download` 只是压缩包目标的别名，下载已被 unzip 隐含。
- 三个可调参数各管一事：`RELEASE_TAG` 固定版本（复现实验环境）、`TOOL=wget` 换下载工具（默认 curl）、`PROXY_USE=true` 走镜像前缀加速。
- `.gitignore` 四行与 make 产物一一对应：`STD_cell` 下的 liberty、全部 gds、压缩包、`.mk` 片段都不进 git——其中第一条把 IO 库 6 个小 liberty 特意留在 git 内，体现了「按体积划界」的取舍。

## 7. 下一步学习建议

- **下一讲（u2-l1）**将进入 PDK 的第一份实质性工艺数据：[prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) 的金属栈与层规则——那是布局布线工具真正「吃」进去的第一类文件。
- 若想巩固本讲的 make 知识，建议通读 GNU make 手册的 *Pattern Rules* 与 *Functions for String Substitution and Analysis* 两章，重点理解 `%` 在 `patsubst` 与模式规则中的一致语义。
- 有网络条件时，实际执行一次 `make unzip RELEASE_TAG=v1.10.100`，然后浏览解压出的 liberty 目录结构与 [README.md:L67-L106](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L67-L106) 的目录树对照——本讲所有「待本地验证」的疑问（包内路径结构、liberty 文件名等）都会在那一刻揭晓，也为 u3-l6（liberty 时序库）准备好数据。
