# 配方执行引擎 recipe.sh

## 1. 本讲目标

在 u2-l3 里，我们把 `install_package` 当作一个「调度员」：它做完三分岔决策后，遇到「需要加工」的包就会调一句 `install_recipe`，然后把真正怎么加工的细节丢给了 `recipe.sh`。本讲就钻进这个被丢出去的黑盒。

plum 里大多数包只是一堆数据文件，直接拷贝即可——那是 u2-l3 讲的「直接安装」路径。但有一类包需要**加工**：比如先从网上额外下载一个文件、或修改用户已有的 `default.yaml` 来「启用」某个方案。这种加工动作就叫**配方（recipe）**（u1-l1 已引入概念）。`recipe.sh` 就是配方的执行引擎。

学完本讲，你应当能够：

- 说清一份 `recipe.yaml` 由哪几个可选段组成，以及 `install_recipe` 按什么顺序执行它们。
- 说清 `apply_download_files` / `apply_install_files` 如何用同一套「`print_section` 提段 + `sed` 列表化」模式，分别完成「下载」和「安装」。
- 说清 `apply_patch_files` 最精妙的设计：它**不直接改文件**，而是把 YAML 片段**转写成一段动态生成的 bash 脚本**，再交给 `bash` 执行；并能解释 `patch_file` 如何往目标文件里注入 `__patch:` 段和 `# Rx:` 标记、为什么这套机制是**幂等**的（重复执行不会重复打补丁）。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **配方字符串语法**（u2-l2）：`user/repo@branch:recipe:key=value,...`，以及 `resolve_recipe` 返回配方名、`resolve_recipe_options` 返回选项数组。
- **安装主循环**（u2-l3）：`install_package` 的三分岔决策——显式配方 `<recipe>.recipe.yaml`、默认配方 `recipe.yaml`、直接拷贝；以及 `install_files` 的 `diff` 增量拷贝与 `files_updated` 计数。本讲的 `apply_install_files` 会**复用** `install_files`。
- **模块系统**（u2-l1）：`require 'recipe'` 是**延迟加载**的（u2-l3 第 4.1 节已强调），只有真正走配方路径时才会把 `recipe.sh` source 进来；`styles.sh` 提供 `info / highlight / error / print_item` 等输出函数。

两个本讲会用到、但属于 Bash / 文本处理通用的知识点，先做个通俗铺垫：

- **`sed` 的范围地址 `/start/,/end/`**：`sed -n '/A/,/B/ { ... }'` 表示「从匹配 A 的行到匹配 B 的行」这个范围内执行命令。plum 用它来「切出 YAML 的某一段」。
- **`<<EOF ... EOF` 是 here-document（_hereafter heredoc_）**：bash 的一种多行字符串写法。`cmd <<EOF` 表示「直到下一个独占一行的 `EOF` 为止的所有内容，都作为 `cmd` 的标准输入」。本讲会看到配方引擎**自动生成**这种结构。
- **字符集 `[[:space:]]` / `[^[:space:]#]`**：`[[:space:]]` 匹配任何空白（空格、制表符……）；`[^[:space:]#]` 表示「既不是空白、也不是 `#`」——也就是 YAML 里「顶格写的关键字」（不含注释行）。这是 plum 识别「下一个顶层键」的判据。

## 3. 本讲源码地图

本讲的主角是单文件 `recipe.sh`，配方引擎的全部逻辑都在这里：

| 文件 | 作用 |
| --- | --- |
| [scripts/recipe.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh) | 配方执行引擎。提供 `install_recipe` 入口与 `apply_download_files` / `apply_install_files` / `apply_patch_files` 三个执行器，外加 `print_section` 段落提取器与 `patch_file` 补丁注入器。 |

`recipe.sh` 并非孤立工作，它依赖上游设置好的一组变量，并会复用 `install-packages.sh` 里的一个函数：

| 文件 | 作用（本讲视角） |
| --- | --- |
| [scripts/install-packages.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh) | `install_package` 在三分岔决策里 `require 'recipe'` 并调用 `install_recipe`；其 `install_files` 函数被本讲的 `apply_install_files` 复用。 |
| [scripts/resolver.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh) | 拆解出 `package` / `recipe` / `recipe_options` 等字段，是配方引擎的输入来源。 |
| [scripts/styles.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh) | 提供 `info / highlight / error / print_item` 等输出函数，`recipe.sh` 顶部 `require 'styles'`。 |

> 关于真实 `recipe.yaml` 样例：plum 仓库本身**不含**任何配方文件——它们分布在各个 `rime-<package>` 子仓库里（由各包作者提供）。因此本讲引用的 `recipe.yaml` 示例都**基于 `recipe.sh` 的解析规则推导**而来，并明确标注为「示例代码」，不声称它们是 plum 仓库里的原文件。具体真实样例「待确认」时需到对应 rime 包仓库查阅。

## 4. 核心概念与源码讲解

先建立宏观印象：一份配方文件就是一段带固定小标题的 YAML，`install_recipe` 像「按目录翻章节」一样，依次把每个小标题对应的段落抽出来、交给对应的执行器。

```
install_recipe(recipe_file)
        │
        ├─ check_recipe_info        # 校验文件自报的 Rx 名字是否对得上
        ├─ apply_download_files     # 段落 download_files：从 URL 下载补充文件
        ├─ apply_install_files      # 段落 install_files：把包内文件拷进 output_dir
        └─ apply_patch_files        # 段落 patch_files：改写 output_dir 里已有目标文件
```

注意三个执行器是**可选**的：哪一段在配方文件里没出现就跳过。所以一份最小配方可以只有 `patch_files`，也可以只有 `install_files`。下面三个小节分别拆解这三个最小模块，外加前置的「结构与校验」。

### 4.1 recipe.yaml 的结构与 Rx 校验

#### 4.1.1 概念说明

配方文件本质是一段 YAML，但 plum **不用 YAML 解析器**去读它——而是用 `sed` 按「顶格关键字 + 缩进段落」的视觉结构去切分。这是 plum 一以贯之的「轻量文本处理」风格：不引依赖，靠 YAML 的格式规律（顶层键顶格写、其下属内容缩进）就能切出每一段。

一份配方文件可能包含的段落（全部可选，由各 `apply_*` / `check_*` 函数按需查找）：

```yaml
# 示例代码：基于 recipe.sh 解析规则推导的配方文件结构（非 plum 仓库原文件）
recipe:            # 可选：声明本配方的名字，供 check_recipe_info 校验
  Rx: minimal

download_files:    # 可选：从额外 URL 下载文件到包缓存目录
  - https://example.com/extra.txt
  - renamed.txt::https://example.com/other.txt

install_files:     # 可选：把包目录里匹配的文件安装到 output_dir
  - *.yaml
  - opencc/*.json

patch_files:       # 可选：改写 output_dir 里已有的目标文件（注入 __patch 段）
  default.yaml:
    __patch:
      schema_list/+:
        - schema: luna_pinyin
```

四个段分别由四个函数处理：`recipe` 段由 `check_recipe_info` 读取校验，其余三段由同名 `apply_*` 函数执行。其中负责「切出某一段」的公共零件是 `print_section`。

#### 4.1.2 核心流程

`install_recipe` 是配方的总入口，它的执行顺序固定为四步：

```
install_recipe(recipe_file)
  1. 校验文件存在；拼出展示用的 rx 字符串；逐条打印 recipe_options
  2. check_recipe_info()   # 用 recipe 段里的 Rx: 行核对配方名
  3. apply_download_files() → apply_install_files() → apply_patch_files()
```

`print_section` 是「段落提取器」，它的工作可以画成这样（以提取 `patch_files` 段为例）：

```
输入：整份 recipe.yaml
        │
        ▼  sed -n '/^patch_files:/,/^[^[:space:]#]/ { ... }'
范围：从顶格的 patch_files: 行 → 到下一个「顶格非注释行」（即下一个顶层键）
        │
        ▼  在范围内，只打印「非顶格非注释」的行
输出：patch_files 下属的全部缩进内容（不含 patch_files: 这行本身，也不含下一个顶层键）
```

`check_recipe_info` 则在 `recipe` 段里找形如 `Rx: minimal` 的行，取出 `minimal`，与「用户实际请求的配方名 `${recipe}`」比对：

- 若用户没指定配方名（`${recipe}` 为空，即默认 `recipe.yaml` 路径）→ **跳过校验**。
- 若用户指定了配方名 → 文件里声明的 `Rx:` 必须与之一致，否则报 `Invalid recipe`。

这是一种**完整性自检**：配方文件在内部「自报家门」，plum 验证你调用的确实是你想调用的那个配方，防止文件被放错位置或被误调用。

#### 4.1.3 源码精读

先看总入口 `install_recipe`：

[scripts/recipe.sh:5-25](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L5-L25) — 注意三处细节：

- [第 7-10 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L7-L10)：配方文件不存在就直接 `echo $(error ...) ; exit 1`。
- [第 12 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L12) `local rx="${package}${recipe:+:${recipe}}"`：这是**展示用**的 rx 字符串。`${recipe:+:${recipe}}` 是参数展开——「recipe 非空时替换成 `:recipe`，否则为空」。所以默认配方（recipe 空）时 rx 只显示包名；具名配方时显示 `包名:配方名`。注意它和 4.3 里 `patch_file` 自建的 rx **不是同一个**。
- [第 18-24 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L18-L24)：固定顺序 `check_recipe_info` → `apply_download_files` → `apply_install_files` → `apply_patch_files`。顺序很关键——先下载、再安装、最后打补丁，这样 `patch_files` 改写目标文件时，该装的数据文件已经就位。

再看段落提取器 `print_section`：

[scripts/recipe.sh:27-32](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L27-L32) — 这是整个 `recipe.sh` 最被复用的零件。它是一个**过滤器**：从标准输入读、向标准输出写。两句 sed：

- `/^${section}:/` 匹配顶格的段名行（如 `patch_files:`），`/^[^[:space:]#]/` 匹配「下一个顶格非注释行」。两者构成一个范围。
- 范围内 `/^[^[:space:]#]/ !p` 表示「不打印顶格非注释行」——于是段名行本身和下一个顶层键都被排除，**只留下属的缩进内容**。

> 这就解释了为什么 plum 对缩进敏感：它完全靠「顶格 vs 缩进」来区分段落边界。如果你的配方文件里某个属性行不小心顶格写了，会被误判成「新段落开始」。

接着看校验函数：

[scripts/recipe.sh:34-45](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L34-L45) — `check_recipe_info`。

- [第 35-40 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L35-L40)：用 `print_section 'recipe'` 切出 `recipe` 段，`grep '^[ ]*Rx: '` 找到 Rx 声明行，再用一长串 `sed` 把 `Rx:` 后面的值抠出来（顺带剥掉首尾的引号和空格）。其中 `'"'"'` 是 bash 在单引号串里嵌入单引号的标准写法（`'\''` 的等价变形），用来在 sed 表达式里匹配单引号 / 双引号。
- [第 41-44 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L41-L44)：`[[ -z "${recipe}" ]] || [[ "${recipe_decl}" == "${recipe}" ]] || ( ... exit 1 )`。三个条件用 `||` 串起来——前两个任一为真就短路跳过，只有「用户指定了配方名」**且**「声明值与请求值不符」时才进入报错子 shell。这就是 4.1.2 说的「默认配方跳过校验」。

#### 4.1.4 代码实践

**实践目标**：亲眼验证 `print_section` 的「段名行不输出、下属缩进行输出」行为。

**操作步骤**：

1. 把下面这段保存为 `/tmp/demo-recipe.yaml`（**示例代码**）：
   ```yaml
   recipe:
     Rx: minimal

   install_files:
     - a.yaml
     - b.yaml

   patch_files:
     default.yaml:
       __patch: hello
   ```
2. 加载 recipe 模块以拿到 `print_section`，然后分别提取三段：
   ```bash
   cd /path/to/plum              # 改成你本地的 plum 仓库路径
   source scripts/bootstrap.sh
   require 'styles'
   require 'recipe'

   for sec in recipe install_files patch_files; do
       echo "===== $sec ====="
       print_section "$sec" < /tmp/demo-recipe.yaml
   done
   ```

**需要观察的现象**：每段的输出都**不含段名行本身**（不会出现 `install_files:`），也**不含下一个顶层键**；`install_files` 段只输出两行 `- a.yaml` / `- b.yaml`，`patch_files` 段只输出 `default.yaml:` 及其下属缩进内容。

**预期结果**：如上。这说明 `print_section` 精确地「抠出了段内缩进体」。若输出里混进了段名行或下一个段名，说明 YAML 缩进写错了（某行没缩进）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `check_recipe_info` 在「默认配方 `recipe.yaml`」路径下根本不校验 `Rx:`？

**答案**：默认配方是包作者声明的「无脑自动执行」的加工方式，用户并没有在命令行点名某个配方名，所以 `${recipe}` 为空。[第 41 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L41) 的 `[[ -z "${recipe}" ]]` 直接为真、短路通过，跳过校验。校验只对「用户显式点名 `:recipe`」的场景有意义——那时才有「请求名 vs 文件自报名」一致性可言。

**练习 2**：`install_recipe` 里 [第 12 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L12) 的 `rx` 和 `patch_file` 里 [第 167 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L167) 的 `rx` 是同一个变量吗？用途相同吗？

**答案**：不是同一个，用途也不同。`install_recipe` 的 `rx` 形如 `package` 或 `package:recipe`（`${recipe:+...}` 决定是否带配方名），只用于日志展示和错误提示；`patch_file` 的 `rx` 形如 `package:recipe:options`（**恒定三段、用 `:` 连**，选项以 `,` 分隔），用作写进目标文件的 `# Rx:` 标记，是补丁幂等性的关键身份标识。

---

### 4.2 download / install 文件处理

#### 4.2.1 概念说明

这是配方引擎里最直观的两个执行器。它们解决两个问题：

- **`apply_download_files`**：有些配方需要包**之外**的文件（比如一个体积很大、不适合放进 git 仓库的词频表）。配方作者在 `download_files` 段列出 URL，plum 就用 `curl` 把它们下载到**包缓存目录** `package_dir`，随后的 `install_files` 就能像对待包内文件一样安装它们。
- **`apply_install_files`**：把包目录里**匹配某种模式**的文件安装到 `output_dir`。它和 u2-l3 讲的「直接安装」用的是**同一个** `install_files`——也就是说，配方驱动的安装同样享受 `diff` 增量拷贝与 `files_updated` 计数。

这两个执行器共用一套**几乎完全相同**的处理模式，值得对照着看：

```
apply_xxx_files()
  1. grep -q '^xxx_files:' recipe_file  →  没有这一段就直接 return（可选段）
  2. cat recipe_file | print_section 'xxx_files' | sed '/^[ ]*#/ d; s/^[ ]*-[ ]//'
     →  得到一个「条目数组」 file_patterns（删注释、剥掉列表的 "- " 前缀）
  3. 数组为空 → return
  4. 对每个条目执行动作：
        download 分支 → download_file（curl 到 package_dir）
        install  分支 → cd package_dir && ls 模式 → install_files 结果
```

唯一的差别只在第 4 步的「动作」上。这种「模式相同、动作不同」的结构，是识别这两个函数的最佳切入点。

#### 4.2.2 核心流程

先看 `download_file` 处理单个 URL 的细节，它支持一种「**给下载文件改名**」的语法：

```
download_file(url)
  filename = get_filename(url)
        │
        ├─ url 形如 "本地名::真实URL"  →  取 ":: " 之前作为 filename
        └─ 否则                        →  取 URL 最后一个 "/" 之后的部分
        │
  若 package_dir/filename 已存在 → 用 curl 的 -z（按时间戳判断是否更新）
  否则                           → 直接下载
```

`-z filename` 是 curl 的「条件请求」：让服务器只在远端比本地 `filename` 更新时才返回内容，避免重复全量下载。这和 u2-l3 的 `diff` 增量思想一脉相承——**能不传就不传**。

再看 `apply_install_files` 与 `install_files` 的衔接：

```
apply_install_files()
  file_patterns = (从 install_files 段抽出的模式，如 *.yaml、opencc/*.json)
  (
    cd package_dir
    ls ${file_patterns[@]}        # 在包目录里展开 glob，得到实际存在的文件名
  )
  → 把这些文件名喂给 install_files   # 复用 u2-l3 的增量拷贝
```

注意 `ls ${file_patterns[@]}` 是在子 shell `( cd ... )` 里执行的，所以 `install_files` 收到的是「**实际匹配到的文件名列表**」而非模式本身。如果某个模式一个文件都没匹配到，`ls` 会往 stderr 打印错误（[第 111-112 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L111-L112) 的 `|| echo $(error ...)`），但不会中断整个安装。

#### 4.2.3 源码精读

先看「改名」与「下载」两个小零件：

[scripts/recipe.sh:47-55](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L47-L55) — `get_filename`。[第 50 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L50) `[[ "$url" = *::* ]]` 检测 `::` 分隔符：有则取前半（`${url%%::*}`）作为本地文件名；无则取 URL 末段（`${url##*/}`）。这就是「`本地名::URL`」改语法的实现。

[scripts/recipe.sh:57-73](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L57-L73) — `download_file`。[第 63-66 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L63-L66) 依文件是否已存在切换措辞（`Downloading` vs `Checking for update of external file`）并决定是否带 `-z`。[第 71 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L71) `curl -fRL -o "$filename" $check_update "${url#*::}"`：`-f` 失败即返回非零、`-R` 把远端时间戳写回本地文件（让下次 `-z` 能用）、`-L` 跟随重定向；`${url#*::}` 取 `::` 之后作为真实 URL（没有 `::` 时就是整个 url）。

现在对照看两个 `apply_*` 执行器——注意它们**共享同一套段落抽取模式**：

[scripts/recipe.sh:75-94](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L75-L94) — `apply_download_files`。

[scripts/recipe.sh:96-117](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L96-L117) — `apply_install_files`。

两者的骨架完全对称，可逐行对照：

| 步骤 | download（L75-94） | install（L96-117） |
| --- | --- | --- |
| 探段 | `grep -q '^download_files:'` | `grep -q '^install_files:'` |
| 抽条目 | `print_section 'download_files' \| sed '/^[ ]*#/ d; s/^[ ]*-[ ]//'` | `print_section 'install_files' \| sed ...`（同一句） |
| 空判 | `(( ${#file_patterns[@]} == 0 )) && return` | 同 |
| 动作 | `download_file $_item`（curl 进 `package_dir`） | `install_files $( cd package_dir && ls 模式 )` |

其中那条共用 `sed`（[第 82-83 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L82-L83) 与 [第 102-103 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L102-L103)）做两件事：`/^[ ]*#/ d` 删掉 YAML 注释行；`s/^[ ]*-[ ]//` 把列表项 `- xxx` 的前缀剥掉，只留 `xxx`。结果被 `$(...)` 拆成数组——这是 plum 解析 YAML 列表的惯用法（不靠 YAML 解析器，靠「列表项都以 `- ` 开头」的格式约定）。

[第 109-116 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L109-L116) 是 `apply_install_files` 与 u2-l3 的衔接点：它把模式交给 `install_files`，后者正是 u2-l3 第 4.3 节那个 `diff` 增量拷贝函数。所以配方里 `install_files` 段安装的文件，**同样会**打印 `Installing:` / `Updating:`、同样会计入 `files_updated`。这是「配方路径」与「直接安装路径」在落盘逻辑上的统一。

> 顺带一提：正因 `apply_install_files` 复用了 `install_files`，而 `install_files` 定义在 `install-packages.sh` 里，所以 `recipe.sh` **必须**从 `install_package` 内部被加载（那时 `install_files` 已在作用域里），它不能脱离 `install-packages.sh` 独立运行。这也是 u2-l3 强调 `require 'recipe'` 是「延迟加载」的深层原因之一。

#### 4.2.4 代码实践

**实践目标**：验证 `download_file` 的「改名语法」与 `-z` 增量下载行为。

**操作步骤**：

1. 准备实验室目录并加载模块：
   ```bash
   lab=/tmp/plum-dl-lab; rm -rf "$lab"; mkdir -p "$lab/pkg"
   cd /path/to/plum                      # 改成你本地 plum 路径
   source scripts/bootstrap.sh; require 'styles'; require 'recipe'
   package_dir="$lab/pkg"
   ```
2. 用改名语法下载一个小文件（任选一个稳定可访问的纯文本 URL；下方为占位，**待本地替换**为可达 URL）：
   ```bash
   download_file "renamed.txt::https://example.com/robots.txt"
   ls -l "$lab/pkg"
   ```
3. **立即再运行一次**同一条命令，观察输出第一行从 `Downloading file:` 变成 `Checking for update of external file:`。

**需要观察的现象**：第一次生成 `renamed.txt`（而非 `robots.txt`，证明改名生效）；第二次走 `-z` 条件下载分支。

**预期结果**：如上。若网络受限无法访问外网，可退化为「源码阅读型实践」：只读 [recipe.sh:57-73](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L57-L73)，在 [第 64 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L64) 标注「文件已存在时 `check_update` 被设为 `-z $filename`」，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`download_files` 段里写 `- renamed.txt::https://example.com/x`，最终落盘的文件叫什么？为什么 `download_file` 里用的是 `${url#*::}` 而不是 `${url%%::*}`？

**答案**：落盘文件叫 `renamed.txt`。`get_filename` 用 `${url%%::*}`（取 `::` **之前**）得到本地文件名；而 `curl` 实际请求的 URL 用 `${url#*::}`（取 `::` **之后**）——两者一个取头、一个取尾，合起来正好把「本地名 :: 真实 URL」拆成两半各取所需。

**练习 2**：`apply_install_files` 里为什么要 `cd "${package_dir}"` 后才 `ls ${file_patterns[@]}`？

**答案**：因为 `file_patterns` 是相对模式（如 `*.yaml`、`opencc/*.json`），必须在包目录里展开才能匹配到正确的文件。`( cd "${package_dir}" && ls ... )` 在子 shell 里切换目录，既展开了 glob，又不影响外层当前目录；展开得到的「实际文件名」再传给 `install_files`，后者用 `package_dir/file` 拼出绝对源路径。

---

### 4.3 patch_files 脚本生成：把 YAML 转写成 bash

#### 4.3.1 概念说明

这是本讲最精巧、也最 plum 风格的一段。它解决的问题是：**如何让一个数据包「修改用户已有的配置文件」**。

设想用户已经有一份 `default.yaml`，里面有自己的设置。某个配方想「启用 luna-pinyin 方案」——这需要在用户的 `default.yaml` 里追加一段补丁。但配方不能直接覆盖用户的整份文件（会丢掉用户其它设置），Rime 的约定是：在文件末尾维护一个 `__patch:` 段，里面列出要打的补丁，Rime 引擎加载时会应用这些补丁。

`patch_files` 段就是配方作者声明「要往哪些目标文件、注入什么补丁内容」。而 `apply_patch_files` 处理它的方式非常特别：

> 它**不直接用 sed 改目标文件**，而是把 `patch_files` 段的 YAML 片段，**转写成一段合法的 bash 脚本**（每个目标文件变成一句 `patch_file 文件名 <<EOF ... EOF` 调用），再把这段脚本**管道喂给 `bash` 执行**。

为什么要绕这一层「生成代码再执行」？因为补丁内容是任意多行文本，bash 的 heredoc 天然适合「把一段多行文本喂给一个函数」。于是 `apply_patch_files` 的角色是**代码生成器**，真正改文件的工作交给 `patch_file` 函数。这种「YAML → bash 脚本 → 执行」的三段式，是 plum 里最值得品味的技巧。

而 `patch_file` 自己则负责把补丁内容**幂等**地注入目标文件的 `__patch:` 段——它会用 `# Rx:` 标记给每块补丁盖上「身份章」，重复执行同一配方时先删旧块再添新块，于是无论跑多少次结果都稳定。

#### 4.3.2 核心流程

先看 `apply_patch_files` 这台「代码生成器」的工作：

```
apply_patch_files()
  1. grep -q '^patch_files:' → 没有就 return
  2. 拼一段 script_header（设置 bash 环境 + 导出 output_dir/package/recipe/recipe_options）
  3. cat recipe_file
        | print_section 'patch_files'      # 切出 patch_files 段
        | sed '...'                         # 改写成 bash 脚本
        | bash                              # 执行生成的脚本
```

其中那段 `sed`（[第 137-148 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L137-L148)）是核心，它对 YAML 做四步改写。以这段配方为例：

```yaml
patch_files:
  default.yaml:
    __patch:
      schema_list/+:
        - schema: luna_pinyin
  squirrel.yaml:
    __patch:
      - schema_list/+: ...
```

经过 `print_section` 后（已剥掉 `patch_files:` 行），喂给 sed 的是各目标文件的缩进块。sed 四步动作：

```
①  1 i\           在最前面插入 script_header（bash 头 + 变量赋值）
②  s/^[ ][ ]//    每行去掉开头的两个空格（整体少缩进一级）
③  s/^\([^[:space:]#]*\):\s*$/patch_file \1 <<EOF/
    把「顶格的名字加冒号」行  default.yaml:
    改写成            patch_file default.yaml <<EOF
④  2,$ { /<<EOF/ i\ EOF }   每个「新的 patch_file 行」之前，插入一个 EOF（关闭上一个 heredoc）
    $ a\ EOF            末尾再追加一个 EOF（关闭最后一个 heredoc）
```

最终被改写成这样一段 bash 脚本（示意）：

```bash
#!/usr/bin/env bash
source '/path/to/scripts/bootstrap.sh'
require 'recipe'
output_dir='/path/to/rime-dir'
package='rime-luna-pinyin'
recipe='enable-schema'
recipe_options=( schema_id=luna_pinyin )
eval ${recipe_options[@]}
# patch files
patch_file default.yaml <<EOF
  __patch:
    schema_list/+:
      - schema: luna_pinyin
EOF
patch_file squirrel.yaml <<EOF
  __patch:
    - schema_list/+: ...
EOF
```

于是「YAML 里声明了几个目标文件」就变成了「生成几句 `patch_file` 调用」。这段脚本被 `| bash` 执行后，控制权就交到了 `patch_file` 手里。

> 这里有一个精妙的细节：为什么第 ④ 步用 `2,$`（从第 2 行起）来判断「要不要在 `<<EOF` 前插 EOF」？因为 `print_section` 的**第 1 行一定是第一个目标文件名**，它会被第 ③ 步变成第一个 `patch_file ... <<EOF`。这是**第一处** heredoc 的开启，它前面不需要、也不能有一个多余的 `EOF`（否则 bash 会把孤立的 `EOF` 当成命令报错）。所以「关闭上一个 heredoc」的逻辑只对**第 2 个及以后**的 `patch_file` 生效——第一个 heredoc 只靠末尾的 `$ a\ EOF` 关闭。把「首行特殊」这个边界处理得如此干净，是这段 sed 的点睛之笔。

再看 `patch_file` 如何**幂等地**注入补丁：

```
patch_file(file_name)
  target_file = output_dir/file_name
  ① 若目标文件不存在 或 不含 '__patch:' 行 → 追加一行 '__patch:'
  ② 算出 rx = package:recipe:options   # 补丁的「身份章」
  ③ 若目标里已有 "# Rx: {rx}" 标记 → 先用 sed 删掉这块旧补丁（从 '# Rx: rx {' 到 '# }'）
  ④ 从 stdin 读补丁内容（heredoc），用 sed 把它插到 '__patch:' 段的末尾，
     并裹上 '# Rx: rx {' 和 '# }' 两行标记
```

关键在 ③+④ 的组合：**先删后插**。因为每块补丁都被唯一的 `# Rx: rx` 标记裹住，重跑同一配方时，③ 会精确删掉上次注入的那块，④ 再注入最新的——所以无论跑 1 次还是 100 次，目标文件里同一配方的补丁永远只有一份。这就是「幂等」。

#### 4.3.3 源码精读

先看 `apply_patch_files` 的脚本头与改写管道：

[scripts/recipe.sh:119-153](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L119-L153) — 整个代码生成器。

[第 123-134 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L123-L134) 的 `script_header`：这是生成脚本的开场白——`source bootstrap.sh`、`require 'recipe'`（这样生成脚本里就能调用 `patch_file`），并把当前配方的上下文（`output_dir` / `package` / `recipe` / `recipe_options`）原样导出。注意第 133 行 `eval \${recipe_options[@]}`：`recipe_options` 形如 `schema_id=luna_pinyin`，`eval` 会把它当成赋值执行，于是配方选项就变成了生成脚本里的 shell 变量，供补丁内容里引用（具体如何引用取决于配方作者）。

[第 135-149 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L135-L149) 是改写管道，已在 4.3.2 逐条对照过。额外注意 [第 139 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L139) `escape_sed_text <<<"${script_header}"`：script_header 含单引号和特殊字符，必须先转义才能安全塞进 sed 的 `i\`（insert）指令——这正是 [第 155-157 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L155-L157) `escape_sed_text` 的职责（给反斜杠和空格加保护、给每行末尾续上 `\` 以满足 sed 多行 `i\` 语法）。最末 [第 149 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L149) `| bash` 才是真正「按下执行键」。

再看真正改写文件的 `patch_file`：

[scripts/recipe.sh:159-198](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L159-L198) — 补丁注入器。

- [第 162 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L162) `target_file="${output_dir:-.}/${file_name}"`：目标文件在 `output_dir` 下；`:-.` 兜底为当前目录。
- [第 163-165 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L163-L165)：若目标不存在或还没有 `__patch:` 段，就先 `>>` 追加一行 `__patch:`——这就是「plum 只追加补丁段、不覆盖整份用户文件」的实现，呼应 u1-l1 讲过的边界。
- [第 166-167 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L166-L167) `option_list="${recipe_options[*]}"`（数组用空格连成串）、`rx="${package}:${recipe}:${option_list// /,}"`（空格再换成逗号）。所以 `# Rx:` 标记恒为 `包名:配方名:opt1,opt2` 三段式。
- [第 168-174 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L168-L174)：幂等性的「删旧」步骤。`grep -Fq "# Rx: ${rx}"` 判断这块补丁是否已存在；若存在，[第 171 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L171) 的 `sed '/^# Rx: '"${rx//\//\\/}"' {$/,/^# }$/ d'` 把「从 `# Rx: rx {` 到 `# }`」这一整块删掉（`${rx//\//\\/}` 把 rx 里的 `/` 转义，避免路径斜杠干扰 sed 地址）。结果写入 `.new` 再 `mv` 覆盖——这是「先删」。
- [第 176 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L176) `patch_contents="$(escape_sed_text)"`：从**标准输入**读取补丁内容（就是 heredoc 喂进来的多行文本）并转义。
- [第 177-193 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L177-L193)：这是「后插」步骤。sed 找到 `__patch:` 段（范围 `/^__patch:$/,/^[^[:space:]#]/`），在其**末尾**（由 `$` 地址限定文件最后一行落在段内）插入裹好标记的补丁块——形如：

  ```
  # Rx: rime-luna-pinyin:enable-schema:schema_id=luna_pinyin {
    __patch:
      schema_list/+:
        - schema: luna_pinyin
  # }
  ```

  其中 `a\`（append，追加到匹配行之后）与 `i\`（insert，插到匹配行之前）的二选一，取决于最后一行本身是不是 `__patch:` 或空白/注释行（[第 180 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L180) 的条件）——保证补丁块总是落在段内、而非把已有内容顶歪。

把 ③ 删旧 + ④ 后插合起来看：目标文件里同一 `rx` 的补丁块**永远至多一份**，且永远是最新内容。这就是幂等。

#### 4.3.4 代码实践

这是本讲的主实践：亲手写一个最小 `recipe.yaml`，驱动 `apply_patch_files` 跑完「YAML → bash 脚本 → 注入 `__patch`」全链路，并检查 `# Rx:` 标记。

> 说明：`recipe.sh` 是按 `require/provide` 设计的可加载模块，所以我们**不必**走完整安装器，只要 `source bootstrap.sh` 后 `require 'recipe'`，再设置好它依赖的全局变量，就能直接调用 `apply_patch_files`。下面所有自制脚本均标注为「示例代码」。

**实践目标**：验证「patch_files 段被转写成 bash 脚本」「目标文件被注入 `__patch:` 段与 `# Rx:` 标记」「重复执行结果幂等」。

**操作步骤**：

1. 准备实验室目录与一个最小目标文件（模拟用户已有的 `default.yaml`）：
   ```bash
   lab=/tmp/plum-recipe-lab; rm -rf "$lab"; mkdir -p "$lab/out"
   cat > "$lab/out/default.yaml" <<'YAML'
   __patch:
   YAML
   ```
2. 写一个最小配方文件（**示例代码**，基于 4.1 的结构规则；含 `install_files` 与一个 `patch_files`）：
   ```bash
   cat > "$lab/minimal.recipe.yaml" <<'YAML'
   recipe:
     Rx: minimal

   patch_files:
     default.yaml:
       __patch:
         schema_list/+:
           - schema: luna_pinyin
   YAML
   ```
3. 加载 recipe 模块并设置它依赖的全局变量，然后执行 `apply_patch_files`：
   ```bash
   cd /path/to/plum                       # 改成你本地 plum 路径
   source scripts/bootstrap.sh
   require 'styles'
   require 'recipe'

   recipe_file="$lab/minimal.recipe.yaml"
   script_dir="$(pwd)/scripts"            # 生成脚本里会 source 它下面的 bootstrap.sh
   output_dir="$lab/out"
   package="my-demo-pkg"
   recipe="minimal"
   recipe_options=(schema_id=luna_pinyin)

   apply_patch_files                      # 内部：cat | print_section | sed | bash
   ```
4. 检查注入结果：
   ```bash
   cat "$lab/out/default.yaml"
   ```

**需要观察的现象**：目标文件 `default.yaml` 里，原来的 `__patch:` 行下方被插入了一块补丁，且被 `# Rx: my-demo-pkg:minimal:schema_id=luna_pinyin {` 与 `# }` 两行标记裹住；`rx` 三段分别是 `package` / `recipe` / `recipe_options`（逗号连接）。

**预期结果**（大致形态）：
```
__patch:
# Rx: my-demo-pkg:minimal:schema_id=luna_pinyin {
  __patch:
    schema_list/+:
      - schema: luna_pinyin
# }
```

5. **幂等验证**：把步骤 3 的 `apply_patch_files` **再执行一次**，然后再次 `cat "$lab/out/default.yaml"`。预期补丁块**仍然只有一份**（因为 [第 168-174 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L168-L174) 先删掉了旧的、[第 177-193 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L177-L193) 才插入新的）。输出会多一行 `Updating patch.` 提示。

**预期结果**：重跑后补丁块数量不变（仍为 1），内容与第一次一致。

> **想直接看到「生成的 bash 脚本」而不执行它**：把 [scripts/recipe.sh:149](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L149) 的 `| bash` 临时改成 `| cat -n`（在你本地的实验副本上改，勿改原文件），再跑步骤 3，就能看到 `apply_patch_files` 到底生成了怎样的 `patch_file ... <<EOF ... EOF` 脚本——这能直观印证 4.3.2 描述的改写过程。看完改回即可。或用下面这段**示例代码**单独预览转换管道（复用 recipe.sh 的 `print_section` / `escape_sed_text`，仅末尾把 `bash` 换成 `cat -n`）：
>
> ```bash
> # 示例代码：预览 apply_patch_files 生成的脚本（不执行）
> preview_patch_script() {
>   local script_header="#!/usr/bin/env bash
> source '${script_dir}/bootstrap.sh'
> require 'recipe'
> output_dir='${output_dir}'
> package='${package}'
> recipe='${recipe}'
> recipe_options=(
>     ${recipe_options[*]}
> )
> eval \${recipe_options[@]}
> "
>   cat "${recipe_file}" | print_section 'patch_files' | sed '{
>       1 i\
> '"$(escape_sed_text <<<"${script_header}")"'
> # patch files
>       s/^[ ][ ]//
>       s/^\([^[:space:]#]*\):\s*$/patch_file \1 <<EOF/
>       2,$ { /<<EOF/ i\ EOF }
>       $ a\ EOF
>   }' | cat -n
> }
> # 在步骤 3 的变量都已设好的前提下：
> preview_patch_script
> ```

#### 4.3.5 小练习与答案

**练习 1**：`apply_patch_files` 为什么要把 YAML「转写成 bash 脚本再执行」，而不是直接在 `apply_patch_files` 里用 sed 改目标文件？

**答案**：因为补丁内容是任意多行、且可能有多个目标文件。bash 的 heredoc 天然适合「把一段多行文本喂给某个函数」，于是把每个目标文件改写成一句 `patch_file 名字 <<EOF ... EOF`，就能用最自然的方式把「补丁内容」传给「改文件的那个函数」。这把职责切得很干净：`apply_patch_files` 只管「生成调用」、`patch_file` 只管「怎么改一个文件」，两者用 heredoc/stdin 解耦。直接在一个函数里又切段落又改文件，会把两种复杂度搅在一起。

**练习 2**：如果同一配方的 `patch_files` 被执行了 3 次，目标文件里这块补丁会有几份？为什么？

**答案**：只有 1 份。因为 [patch_file 第 168-174 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L168-L174) 会先用 `# Rx: {rx}` 标记定位并删掉旧块，[第 177-193 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L177-L193) 再插入新块。`rx` 由 `package:recipe:options` 唯一决定，所以同一配方（含相同选项）的补丁身份章固定，先删后插保证幂等。只有当 `recipe_options` 不同（`rx` 随之不同）时，才会被视为两块不同补丁并存。

**练习 3**：[第 143 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L143) 的「关闭上一个 heredoc」逻辑为什么用 `2,$` 限定「第 2 行起」？如果改成 `1,$`（对所有 `<<EOF` 行都先插一个 `EOF`）会出什么问题？

**答案**：`print_section` 输出的第 1 行必然是第一个目标文件名，它被改写成第一个 `patch_file ... <<EOF`——这是首个 heredoc 的**开启**，它前面不该有 `EOF`。若改成 `1,$`，会在第一句 `patch_file` 之前凭空多出一个孤立的 `EOF` 行，bash 会把它当成命令去执行（`EOF: command not found`）并打乱 heredoc 配对。用 `2,$` 正好让「关闭上一个」只作用于「第 2 个及以后的 `patch_file`」，首个 heredoc 仅靠末尾 `$ a\ EOF` 关闭。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的小任务：**从零写一份包含三段的配方，并解释它触发的每一段代码路径**。

**任务**：在实验室目录里构造一个假包，写一份同时含 `download_files`（可选/可跳过）、`install_files`、`patch_files` 的 `recipe.yaml`，手动驱动 `install_recipe` 完整跑一遍，再用本讲学到的词汇解释每一步输出。

**步骤**：

1. 准备实验室目录与一个假包，包里放一个待安装的 yaml：
   ```bash
   lab=/tmp/plum-recipe-full; rm -rf "$lab"; mkdir -p "$lab/pkg" "$lab/out"
   echo "config_version: 1.0" > "$lab/pkg/my-data.yaml"
   # 目标文件预先存在，用于演示 patch_files 的「追加 __patch」而非覆盖
   printf '__patch:\n' > "$lab/out/default.yaml"
   ```
2. 写配方文件（**示例代码**）：
   ```bash
   cat > "$lab/full.recipe.yaml" <<'YAML'
   recipe:
     Rx: full

   install_files:
     - my-data.yaml

   patch_files:
     default.yaml:
       __patch:
         schema_list/+:
           - schema: my_data
   YAML
   ```
3. 加载模块、设置变量、调用 `install_recipe` 全流程：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'styles'
   require 'recipe'

   recipe_file="$lab/full.recipe.yaml"
   script_dir="$(pwd)/scripts"
   output_dir="$lab/out"
   package_dir="$lab/pkg"
   package="my-demo"
   recipe="full"
   recipe_options=()
   files_updated=0                       # install_files 会自增它（u2-l3）

   install_recipe "$recipe_file"
   ```
   > 注意：本步骤复用了 `install-packages.sh` 的 `install_files`。若你仅 `require 'recipe'` 而未定义它，`apply_install_files` 会在调用时报 `install_files: command not found`。两种解法：① 从 `install-packages.sh` 里把 `install_files` / `create_containing_directory` 两个函数定义复制进你的实验脚本（**示例代码**，仅用于练习）；② 直接走真实安装器端到端验证（见下）。
4. 对照解释输出里出现的每一类信息：
   - `Installing recipe: my-demo:full` → [install_recipe 第 13 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L13) 的展示用 rx。
   - `Installing: my-data.yaml` → `apply_install_files` 复用 `install_files` 的「目标不存在」分支（u2-l3 4.3）。
   - `Patching: default.yaml` → [patch_file 第 161 行](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L161)。
5. 检查 `cat "$lab/out/default.yaml"`：应能看到被注入的 `# Rx: my-demo:full: { ... }` 补丁块（注意 `recipe_options` 为空时 rx 形如 `my-demo:full:`）。
6. （端到端替代方案）若想跳过手工拼装，可改用真实安装器：把上面的 `full.recipe.yaml` 放进一个**已用 git 管理的本地假包**目录（`git init && git add . && git commit`），再放到 plum 的 `package/<user>/<name>/` 缓存位置，然后 `rime_dir=$lab/out bash rime-install <name>:full`。这一路会经过 u2-l3 的 `install_package` 三分岔 → `install_recipe`，完整复现本讲链路（具体能否跑通「待本地验证」，取决于 `update-package.sh` 对本地 git 仓库的处理）。

**预期结果**：你能不看讲义，用「`print_section` 切段」「`apply_install_files` 复用 `install_files`」「`apply_patch_files` 生成 bash 脚本」「`patch_file` 注入 `__patch` + `# Rx:` 标记、先删后插保幂等」这几组词汇，解释步骤 3-5 里每一行输出的来源。

> 若环境无法联网或不愿构造 git 假包，可退化为「源码阅读型实践」：只读 [scripts/recipe.sh:5-198](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/recipe.sh#L5-L198)，在纸上为步骤 2 的配方逐段标注它会命中哪些行号，标注「待本地验证」。

## 6. 本讲小结

- 配方文件是一段按「顶层键 + 缩进段」组织的 YAML，plum **不引 YAML 解析器**，靠 `print_section`（一个 sed 范围过滤器）按段名切出属下缩进内容。
- `install_recipe` 按固定顺序执行 `check_recipe_info` → `apply_download_files` → `apply_install_files` → `apply_patch_files`；每段都是可选的，缺段即跳过。
- `check_recipe_info` 用 `recipe` 段里的 `Rx:` 行核对配方名，仅对「用户显式点名配方」的场景生效，默认 `recipe.yaml` 路径跳过校验。
- `apply_download_files` 与 `apply_install_files` 共用同一套「`print_section` 提段 + `sed` 列表化」模式：前者用 `download_file`（curl `-fRL`、支持 `本地名::URL` 改名、`-z` 增量）下载到包缓存；后者把 glob 展开结果喂给 u2-l3 的 `install_files`，享受 `diff` 增量与 `files_updated` 计数。
- `apply_patch_files` 是一台**代码生成器**：用一段 sed 把 `patch_files` 段的 YAML 改写成合法 bash 脚本（每个目标文件 → 一句 `patch_file 名字 <<EOF ... EOF`），再 `| bash` 执行；首行特殊处理（`2,$`）保证首个 heredoc 不被多余 `EOF` 干扰。
- `patch_file` 往目标文件追加 `__patch:` 段（不覆盖整份用户文件），用三段式 `# Rx: package:recipe:options` 标记裹住每块补丁，**先删后插**实现幂等——重复执行同一配方，补丁永远至多一份。

## 7. 下一步学习建议

本讲把 `install_recipe` 的内部细节讲透了，它和上下游还有几条值得继续追的线索：

- **想看「配方文件从哪来、怎么被点名」**：回顾 u2-l2 的 `resolve_recipe` / `resolve_recipe_options`（[resolver.sh:40-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/resolver.sh#L40-L56)），以及 u2-l3 的 `install_package` 三分岔决策（[install-packages.sh:38-46](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/install-packages.sh#L38-L46)）——它们共同决定了「何时、用哪个 `recipe_file` 调用本讲的 `install_recipe`」。
- **想搞清「包清单与预设集合」**：继续学 **u2-l7（配置包清单与预设集合）**，看 `preset/extra/all-packages.conf` 如何定义 `package_list` 数组、`load_package_list_from_target` 如何把它们 source 进来。
- **想动手写真实配方**：到某个 `rime-<package>` 仓库（如 [rime/rime-luna-pinyin](https://github.com/rime/rime-luna-pinyin)）里找现成的 `recipe.yaml` / `*.recipe.yaml`，对照本讲的四段结构（`recipe` / `download_files` / `install_files` / `patch_files`）逐行解读，验证你对其解析规则的理解。具体样例「待确认」以各包仓库实际内容为准。
