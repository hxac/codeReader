# 脚本化构建信息：Python 占位符注入

## 1. 本讲目标

本讲是「版本与编译时间机制」单元的第三篇，聚焦 fpga_base 在 1.4.0 版本引入的**脚本化构建信息**路径。

学完后你应该能够：

1. 看懂 `fpga_base_scripted_info_pkg.vhd` 中 `$$tag$$` 占位符常量的写法，理解「带标签的普通源文件」如何变成可被脚本注入的模板。
2. 读懂 `scripts/update_version.py` 的核心逻辑：用 gitpython 读取 git 提交哈希、用正则表达式把日期和哈希写回 HDL、对脏仓库的特殊处理、以及最后用 `git update-index --assume-unchanged` 隐藏脚本改动。
3. 理解顶层 `C_USE_INFO_FROM_SCRIPT` 这个总闸如何**同时**切换「版本号寄存器」和「固件日期寄存器」两条链路的数据来源。
4. 能清楚说出**脚本化 Python 路径**与上一篇（u3-l2）讲的**传统 TCL 综合钩子路径**在时机、对象、能力上的差异，并知道何时选哪条。

---

## 2. 前置知识

本讲承接 u3-l1 与 u3-l2，默认你已经理解下面这些结论（这里只做最简要点回顾，不再展开）：

- **FDPE 当一位 ROM**：fpga_base 用 160 个 Xilinx 原语 `FDPE` 触发器存固件编译时间（年/月/日/时/分各 32 个），它们的 `D/CE/PRE` 全接常量 `'0'`，靠 `dont_touch` 属性防被综合优化掉，运行期永不翻转，输出恒等于上电初值属性 `INIT`。详见 [hdl/fpga_base_date_package.vhd:102-117](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117)。
- **双模式分支**：日期组件 `fpga_base_date` 有两条互斥的 `if generate`——`g_generics`（`C_USE_GENERIC_DATE=true`）从 generic 常量读日期，`g_ngenerics`（默认）从 FDPE 的 Q 读。详见 [hdl/fpga_base_date_package.vhd:187-203](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L203)。
- **传统 TCL 钩子路径**：根目录 `fpga_base.tcl` 是一段在综合之后、`opt_design` 之前运行的 `tcl.pre` 钩子，它用 `set_property INIT` 逐位改写 FDPE 的初值。详见 [fpga_base.tcl:46-68](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L46-L68)。
- **寄存器映射**：版本号在偏移 `0x00`（`reg_rdata(0)`），固件日期在 `0x04~0x14`（`reg_rdata(1~5)`）。

本讲要回答的核心问题是：**如果不走「综合后改 FDPE 的 INIT」这条路，而是想在综合之前就把构建信息写死进 HDL，fpga_base 是怎么做的？答案就是「占位符 + Python 正则替换 + gitpython」这一套。**

> 术语提示：
> - **占位符（placeholder）**：源码里一个形如 `$$year$$` 的标记，本身是注释，不影响编译，但能被脚本精确定位。
> - **gitpython**：一个 Python 库（`pip install gitpython`），让 Python 代码能像 `git` 命令一样查询仓库状态。
> - **assume-unchanged**：git 索引（index）的一个标记，告诉 git「假装这个文件没被改过」，从而不在 `git status` 里显示它的修改。

---

## 3. 本讲源码地图

本讲涉及三个文件，各司其职：

| 文件 | 角色 | 本讲关注点 |
|------|------|------------|
| [hdl/fpga_base_scripted_info_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_scripted_info_pkg.vhd) | **被注入的模板**（VHDL 包） | 6 个带 `$$tag$$` 注释的常量，是 Python 脚本的写入目标 |
| [scripts/update_version.py](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py) | **注入器**（Python 脚本） | 读 git 哈希、取当前时间、正则替换写回 HDL、处理脏仓库、assume-unchanged 收尾 |
| [hdl/fpga_base_v1_0.vhd](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) | **消费方**（顶层） | 用 `C_USE_INFO_FROM_SCRIPT` 总闸决定版本号与日期从哪里来 |

数据流非常清晰：

```
git 仓库 ──┐
           │  update_version.py（综合前手动/编排调用）
当前时间 ──┘        │
                    │  正则替换 $$tag$$
                    ▼
        fpga_base_scripted_info_pkg.vhd（6 个常量被改写）
                    │
                    │  VHDL 编译期常量传播
                    ▼
              fpga_base_v1_0（顶层）
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  reg_rdata(0) 版本号      fpga_base_date 日期组件
  （BuildGitHash_c）       （g_generics 分支读 BuildYear_c 等）
```

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「模板 → 注入器 → 消费方」的顺序递进。

### 4.1 占位符替换模式：让普通文本文件变成可注入模板

#### 4.1.1 概念说明

很多构建系统都需要把「构建时间、版本号、代码哈希」这类**每次构建都不同**的信息塞进源码。常见做法有两类：

1. **代码生成**：用一个模板文件（如 `xxx.in`），构建时渲染成真正的源文件（`xxx.vhd`）。模板与产物分离。
2. **就地替换**：源文件本身就能直接编译，里面留好「占位默认值」，构建时用一个脚本找到标记、改掉值，但文件名和结构都不变。

fpga_base 选的是**第二种**。这样做的好处是：

- 没跑脚本时，`fpga_base_scripted_info_pkg.vhd` 也是合法的、可综合的 VHDL（默认值 `0000`、`X"00000000"` 都合法），不会因为「忘了跑脚本」就编译失败。
- 占位标记写在注释里（`-- $$year$$`），完全不影响 VHDL 语义，只是给脚本一个「锚点」。
- 脚本不需要理解 VHDL 语法，只要会「按行找字符串 + 正则替换」即可，工具无关性极强。

#### 4.1.2 核心流程

`update_version.py` 里的核心替换函数 `ReplaceInTaggedLine` 走的是「**先定位，再替换**」两段式：

1. **定位**：逐行扫描文件，找到**第一个**包含 `$${tag}$$`（如 `$$year$$`）的行。tag 是「锚」，保证改的是对的行。
2. **替换**：在这一行上跑一次正则 `re.sub(expRegex, new, l)`，把「值」那一段精确改掉，行里其它内容（注释、类型声明）原样保留。
3. **回写**：把改过的行列表整体写回文件。
4. **兜底**：如果扫完所有行都没找到 tag，用 Python 的 `for...else` 抛异常，提醒你模板被破坏了。

关键在于「定位用 tag、替换用正则」这**两层解耦**：tag 决定「改哪一行」，正则决定「这一行里改哪一段」。所以哪怕有人把常量重命名、调整缩进，只要注释里的 tag 还在、值的格式还匹配正则，替换就仍然成立。

#### 4.1.3 源码精读

先看被注入的模板。包里声明了 6 个常量，每个常量所在行的末尾注释里都有一个 `$$tag$$` 标记：

[hdl/fpga_base_scripted_info_pkg.vhd:12-17](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_scripted_info_pkg.vhd#L12-L17) —— 6 个带占位符的常量（脚本写入目标）：

```vhdl
constant BuildYear_c    : integer := 0000; -- $$year$$
constant BuildMonth_c   : integer := 0;    -- $$month$$
constant BuildDay_c     : integer := 0;    -- $$day$$
constant BuildHour_c    : integer := 0;    -- $$hour$$
constant BuildMinute_c  : integer := 0;    -- $$minute$$
constant BuildGitHash_c : std_logic_vector(31 downto 0) := X"00000000"; -- $$githash$$
```

注意两类默认值的写法不同：5 个日期是 `integer := 0/0000`，而哈希是 `std_logic_vector := X"00000000"`（VHDL 的位串字面量）。这决定了替换它们的正则也不同（见下）。

再看替换函数本身。[scripts/update_version.py:12-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L12-L28) —— 通用「按 tag 定位、按正则替换」函数：

```python
def ReplaceInTaggedLine(file : str, tag : str, expRegex : str, new : str):
    with open(file, "r") as f:
        contentLines = f.readlines()           # 读全部行
    for idx, l in enumerate(contentLines):
        if "$${}$$".format(tag) in l:          # 1) 找到含 tag 的行
            contentLines[idx] = re.sub(expRegex, new, l)  # 2) 在该行做正则替换
            break
    else:                                       # 3) for...else：一次都没 break ⇒ tag 缺失
        raise Exception("tag '$${}$$' not found in file '{}".format(tag, file))
    with open(file, "w+") as f:
        f.writelines(contentLines)             # 4) 回写
```

> 小知识：Python 的 `for...else` 中，`else` 块**只有在循环没有被 `break` 打断时**才会执行。这里用它实现「找不到 tag 就报错」，非常地道。

主函数里实际调用它时，传的正则分两种。[scripts/update_version.py:77-84](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L77-L84) —— 对 6 个常量分别做替换：

```python
ReplaceInTaggedLine(VERSION_FILE, "githash", r'"[0-9a-fA-F]*"', '"{}"'.format(commitHash8))
...
ReplaceInTaggedLine(VERSION_FILE, "year",   r'= [0-9]*;', '= {};'.format(buildDate.year))
ReplaceInTaggedLine(VERSION_FILE, "month",  r'= [0-9]*;', '= {};'.format(buildDate.month))
ReplaceInTaggedLine(VERSION_FILE, "day",    r'= [0-9]*;', '= {};'.format(buildDate.day))
ReplaceInTaggedLine(VERSION_FILE, "hour",   r'= [0-9]*;', '= {};'.format(buildDate.hour))
ReplaceInTaggedLine(VERSION_FILE, "minute", r'= [0-9]*;', '= {};'.format(buildDate.minute))
```

把两种正则在脑子里跑一遍，就能看到替换的精确性：

- **哈希行**：原文 `:= X"00000000";`。正则 `"[0-9a-fA-F]*"` 只匹配带引号的十六进制串 `"00000000"`，替换成 `"a1b2c3d4"`，结果 `:= X"a1b2c3d4";`。`X` 和分号都没动。
- **日期行**：原文 `:= 0000;`。正则 `= [0-9]*;` 匹配 `= 0000;`（`:=` 里的 `=` 后面那段），替换成 `= 2026;`，结果 `:= 2026;`。

两种正则都只命中各自行里**唯一**的那段值，不会误伤其它内容。

#### 4.1.4 代码实践

这是一个**纯文本追踪型**实践，不需要 Vivado，也不需要真的跑脚本，用纸笔或编辑器即可完成。

1. **实践目标**：亲手验证「tag 定位 + 正则替换」两层逻辑，确认替换结果仍是合法 VHDL。
2. **操作步骤**：
   - 抄下模板里 `BuildGitHash_c` 那一行原文：
     `    constant BuildGitHash_c : std_logic_vector(31 downto 0) := X"00000000"; -- $$githash$$`
   - 假设本次构建的 8 位哈希是 `commitHash8 = "9b249a7a"`。
   - 手动套用脚本的正则：把 `"[0-9a-fA-F]*"` 匹配到的部分替换为 `"9b249a7a"`。
   - 再对 `BuildMinute_c` 行（`:= 0;`）套用 `= [0-9]*;` → `= 37;`（假设当前是第 37 分）。
3. **需要观察的现象**：替换后 `X"..."` 的 `X` 前缀和结尾的 `;`、注释 `-- $$githash$$` 是否都被保留。
4. **预期结果**：
   - 哈希行变为 `... := X"9b249a7a"; -- $$githash$$`（注意 tag 注释还在，因为正则没碰它）。
   - 分钟行变为 `... := 37; -- $$minute$$`。
   - 两行都是合法 VHDL，且 tag 标记依旧存在（所以脚本可以反复运行）。
5. 结论：**tag 是「找行」用的，正则是「改值」用的，二者互不干扰**——这正是该模式可重复执行、鲁棒的原因。

> 待本地验证：如果你本机装了 Python，可以把这一行原样写进一个 `test.txt`，然后 `python -c "import re; ..."` 跑一次 `re.sub(r'\"[0-9a-fA-F]*\"', '\"9b249a7a\"', line)` 自己确认输出。

#### 4.1.5 小练习与答案

**练习 1**：如果把模板里 `-- $$year$$` 这个注释删掉，再跑 `update_version.py`，会发生什么？

> **答案**：`ReplaceInTaggedLine` 的 `for` 循环扫完全部行都找不到含 `$$year$$` 的行，于是走进 `for...else` 的 `else` 分支，抛出异常 `tag '$$year$$' not found in file '...'`，脚本中止。这体现了「tag 是契约」——模板格式不能乱改。

**练习 2**：日期用的正则是 `= [0-9]*;` 而哈希用的是 `"[0-9a-fA-F]*"`，为什么不能统一成一个？

> **答案**：因为两类常量在 VHDL 里的字面量形态不同。整数写成 `:= 0000;`（值是裸数字、夹在 `= ` 和 `;` 之间），而 `std_logic_vector` 写成 `:= X"00000000";`（值是带引号的十六进制、且可能含 `a-f`）。正则必须精确匹配各自的真实文本，否则要么匹配不到、要么误伤别的字符。

---

### 4.2 gitpython 版本读取：从消费方仓库取出可追溯身份

#### 4.2.1 概念说明

光有占位符还不够，得有人把「真实的构建信息」填进去。`update_version.py` 的 `FpgaBaseUpdateVersion` 函数就是干这件事的，它要回答三个问题：

1. **这次的 git 身份是什么？**——从哪个仓库取哈希？
2. **这次的构建时间是什么？**——取编译机当前墙上时间。
3. **这次构建是否「干净可复现」？**——如果仓库里有未提交改动，哈希还能代表真实代码吗？

这里有个**很容易被忽略的设计要点**：函数签名是 `FpgaBaseUpdateVersion(gitRepo : str, ...)`，参数 `gitRepo` 指的是「**要写入版本寄存器的那个仓库**」——也就是**使用 fpga_base 的消费方工程**，而**不是 fpga_base 这个 IP 本身**。

为什么？因为 fpga_base 是一个被多处复用的共享 IP。烧进某块板子的比特流里，版本寄存器应当记录「**这是哪个工程、哪次提交编译出来的**」，而不是「fpga_base 库本身的版本」。这样运维人员读出 `0x00` 寄存器，就能定位到具体的消费方工程提交，实现现场固件的可追溯。

> 当 `__main__` 里以 `FpgaBaseUpdateVersion("..")` 直接运行时，`..` 是 `scripts/` 的上一级，即 fpga_base 仓库根目录——此时消费方恰好就是 fpga_base 自身（自测场景）。在真实工程里，调用方一般会传入自己的工程仓库路径。

#### 4.2.2 核心流程

[scripts/update_version.py:34-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L34-L89) 的执行步骤：

```
1. 导入 gitpython（缺失则给出 pip 安装提示并退出）
2. 打开 gitRepo 指向的消费方仓库（非法则报错）
3. 脏仓库检测：
   ├─ 若 is_dirty(untracked_files=True)：
   │    · 警告「构建自脏仓库不推荐，请先提交」
   │    · 交互询问 Continue / Abort
   │    · 选 Abort ⇒ exit()
   │    · 选 Continue ⇒ 标记 GitUnclean=True，继续
4. 取 git 哈希：
   · commitHash  = repo.head.object.hexsha      （40 位完整 SHA）
   · commitHash8 = commitHash[0:8]              （取前 8 位）
   · 若 GitUnclean：commitHash8 = "FFFFFFFF"     （脏仓库哨兵值）
5. 取当前时间 dt.datetime.now()
6. 把 6 个值写回 fpga_base_scripted_info_pkg.vhd（4.1 节的 ReplaceInTaggedLine）
7. 对 fpga_base IP 仓库执行 git update-index --assume-unchanged（隐藏脚本改动）
```

其中第 3、4、7 步是 git 相关的关键策略，下面精读。

#### 4.2.3 源码精读

**脏仓库检测**。[scripts/update_version.py:55-64](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L55-L64)：

```python
GitUnclean = False
if repo.is_dirty(untracked_files=True):
    print("... Repository is Dirty! ... please commit first.\n")
    GitUnclean = True
    result = input("Continue (c) or Abort (a)?: ")
    if result != "c":
        print("\nBuild Aborted!")
        exit()
    print()
```

`is_dirty(untracked_files=True)` 同时把「已跟踪文件被修改」和「存在未跟踪文件」都算作脏。脏仓库只是**警告 + 二次确认**，并非硬性阻止——给开发者留了「我知道我在干嘛」的逃生口。

**取哈希与脏仓库哨兵**。[scripts/update_version.py:66-72](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L66-L72)：

```python
commitHash = repo.head.object.hexsha        # 40 位完整 SHA
commitHash8 = commitHash[0:8]               # 前 8 位
if GitUnclean:                              # 脏仓库 ⇒ 哈希不再可信
    commitHash8 = "FFFFFFFF"
```

这里有一个**精妙的可追溯性设计**：git 的 40 位 SHA 是对「已提交内容」的指纹。一旦工作区有未提交改动（脏），同样的哈希就可能对应不同的实际代码——哈希失去了「唯一标识本次构建」的能力。于是脚本把脏仓库的 8 位哈希强制改成全 `F`（`FFFFFFFF`）作为**哨兵值**：

\[ \text{gitHash8} = \begin{cases} \text{commitHash}[0:8] & \text{仓库干净} \\ \texttt{FFFFFFFF} & \text{仓库脏} \end{cases} \]

这样运维读寄存器看到 `0xFFFFFFFF` 就立刻知道：**这块固件来自一次不可复现的脏构建**，不能用它反推源码状态。注意 8 位十六进制正好填满 32 位寄存器：\( 8 \times 4 = 32 \)，与 `BuildGitHash_c : std_logic_vector(31 downto 0)` 完全吻合。

**assume-unchanged 收尾**。[scripts/update_version.py:87-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L87-L89)：

```python
ipRepo = git.Repo(FILEPATH + "/..")
ipRepo.git.execute(["git", "update-index", "--assume-unchanged", os.path.abspath(VERSION_FILE)])
```

注意这里用的是 `ipRepo`（`FILEPATH + "/.."`，即 `update_version.py` 所在 `scripts/` 的上一级 = **fpga_base IP 仓库**），而不是前面取哈希的 `gitRepo`（消费方仓库）。因为被改写的文件 `fpga_base_scripted_info_pkg.vhd` 物理上住在 **IP 仓库**里，所以要告诉 **IP 仓库**的 git 索引忽略它的改动。`--assume-unchanged` 让 git 假装这个文件此后没变过。

#### 4.2.4 代码实践

> 这是本讲的**指定实践任务**。

1. **实践目标**：搞清楚 `update_version.py` 对脏仓库做了什么，以及为什么最后要 `assume-unchanged`。
2. **操作步骤**：
   - 阅读 [scripts/update_version.py:55-72](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L55-L72)，回答：当 `repo.is_dirty(untracked_files=True)` 为真时，脚本做了哪三件事？哈希最终被改成什么？
   - 阅读 [scripts/update_version.py:74-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L74-L89)，回答：脚本刚刚用 `f.writelines(contentLines)` 改写了 HDL 文件，紧接着为什么反而要执行 `git update-index --assume-unchanged` 把这个改动「藏起来」？
3. **需要观察的现象 / 预期结果**：
   - **脏仓库处理**：脚本做了 ① 打印警告、② 交互询问 Continue/Abort（选 `a` 则 `exit()` 中止）、③ 若继续则置 `GitUnclean=True` 并在随后把哈希改成哨兵值 `"FFFFFFFF"`。也就是说脏仓库**不强制阻止**构建，但会**抹掉哈希的可追溯性**作为惩罚性标记。
   - **assume-unchanged 的目的**：脚本每次运行都会把当前的哈希和时间**就地写进** `fpga_base_scripted_info_pkg.vhd`。如果不加这一句，这个文件在 `git status` 里就会**永远显示为「已修改」**，带来三个麻烦：
     1. 工作区长期脏乱，干扰其它真正的改动审查；
     2. 容易被人误 `git commit`，把「某次构建的临时哈希」当成正式内容提交进库（而库里的**正确状态应当是占位默认值 `0000`/`X"00000000"`**）；
     3. 让 IP 仓库自己永远 `is_dirty()`，递归地触发本脚本的脏仓库警告。
     
     用 `--assume-unchanged` 标记后，git 不再追踪该文件的后续修改，**库里的提交版本始终是干净的占位模板**，而每次本地构建临时写入的值只存在于本地工作区、不进版本库。这是一个很干净的「模板在库里、值在本地」的分离。
4. 结论：脏仓库处理守护的是「**哈希的可信度**」，assume-unchanged 守护的是「**模板的库内纯洁度**」。

> 待本地验证：若有 gitpython，可在一份测试仓库里 `touch a.txt` 制造未跟踪文件后调用脚本，观察打印与生成的 HDL；再用 `git ls-files -v` 查看被标记为 `h`（assume-unchanged）的文件条目。

#### 4.2.5 小练习与答案

**练习 1**：函数参数 `gitRepo` 与脚本末尾的 `ipRepo` 是不是同一个仓库？为什么这很重要？

> **答案**：不一定相同。`gitRepo`（参数，`__main__` 里传 `..`）是**取哈希的来源**，语义上是「消费方工程」；`ipRepo`（`FILEPATH + "/.."`）是**被改写文件所在的 IP 仓库**。脚本从前者读哈希、改后者里的文件、再对后者做 assume-unchanged。当 fpga_base 自测时二者恰好都是 fpga_base 仓库；但在真实工程里，消费方会把哈希写进共享 IP 的模板文件——这正是「版本寄存器记录消费方身份」设计的体现。

**练习 2**：为什么哨兵值用 `FFFFFFFF` 而不是一个固定的「真实」哈希？

> **答案**：因为 `FFFFFFFF` 在正常 git 提交里几乎不可能作为前 8 位出现，它是一个**一眼可辨的非法标记**，读到它的人立刻知道「这是一次不可复现的脏构建」，不会误把它当成某个真实提交去 `git show` 查找，避免了误导。

---

### 4.3 双信息源切换：一个总闸统管版本号与日期两条链路

#### 4.3.1 概念说明

到目前为止，本单元讲了两条把构建信息送进寄存器的路径：

- **传统 TCL 路径**（u3-l2）：`fpga_base.tcl` 在综合之后改 FDPE 的 `INIT`，只负责**日期**；版本号寄存器放用户手填的语义版本 `C_VERSION`。
- **脚本化 Python 路径**（本讲）：`update_version.py` 在综合之前改 VHDL 常量，同时负责**日期 + git 哈希**。

这两条路径**不能同时生效**，否则版本号和日期会打架。fpga_base 用一个布尔泛型 `C_USE_INFO_FROM_SCRIPT` 当**总闸**，一次性统管两条链路：

- 版本号寄存器 `0x00`：从 `C_VERSION`（用户语义版本）与 `BuildGitHash_c`（脚本注入的哈希）二选一。
- 固件日期 `0x04~0x14`：在日期组件里从「FDPE 的 INIT」与「generic 常量」二选一（即 u3-l1 讲的 `g_generics` / `g_ngenerics` 分支）。

同一个开关同时拨动两处，保证两条链路始终是**同一种来源**，不会出现「版本号是脚本注入、日期却是 TCL 钩子写入」这种不一致。

#### 4.3.2 核心流程

```
顶层 generic: C_USE_INFO_FROM_SCRIPT : boolean := false   （默认走传统 TCL 路径）
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
   reg_rdata(0) 版本号          fpga_base_date 日期组件
            │                         │  C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT
   false ⇒ C_VERSION            false ⇒ g_ngenerics（读 FDPE INIT，由 fpga_base.tcl 写）
   true  ⇒ BuildGitHash_c       true  ⇒ g_generics （读 generic 常量，由 update_version.py 写）
```

两条路径的对照表：

| 维度 | 传统 TCL 路径（`C_USE_INFO_FROM_SCRIPT=false`） | 脚本化 Python 路径（`C_USE_INFO_FROM_SCRIPT=true`） |
|------|------------------------------------------------|----------------------------------------------------|
| 触发脚本 | `fpga_base.tcl` | `scripts/update_version.py` |
| 运行时机 | 综合**之后**（实现阶段 `tcl.pre`） | 综合**之前**（构建编排中手动/预先调用） |
| 修改对象 | 已综合网表里的 FDPE **cell** 的 `INIT` 属性 | VHDL **源码**里的常量值 |
| 依赖前提 | FDPE cell 必须已存在、可被 `get_cells` 寻址 | 仅需源文件可读写、有 `$$tag$$` 标记 |
| 版本号 `0x00` | `C_VERSION`（用户语义版本） | `BuildGitHash_c`（8 位 git 哈希） |
| 日期 `0x04~0x14` | `g_ngenerics` 分支读 FDPE 的 Q | `g_generics` 分支读 `BuildYear_c` 等常量 |
| 是否需要 gitpython | 否 | 是 |
| 典型场景 | 不关心 git 追溯、用语义版本号管理 | 需要把「哪次提交编译的」烧进固件 |

#### 4.3.3 源码精读

先看顶层泛型定义。[hdl/fpga_base_v1_0.vhd:37](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L37) —— 总闸泛型，默认 `false`（即默认走传统 TCL 路径）：

```vhdl
C_USE_INFO_FROM_SCRIPT      : boolean := false;
```

这个泛型也会被 PsiIpPackage 打包成 IP GUI 上的一个可勾选项（详见 [scripts/package.tcl:65](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L65)，显示名为 "Use Build-Info from Python Script (not from Vivado TCL)"），用户在 Vivado 里直接勾选即可切换。

**第一处切换：版本号寄存器**。[hdl/fpga_base_v1_0.vhd:233-234](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L233-L234)：

```vhdl
reg_rdata( 0) <= C_VERSION when not C_USE_INFO_FROM_SCRIPT else 
                 BuildGitHash_c;
```

- `C_USE_INFO_FROM_SCRIPT=false` ⇒ `reg_rdata(0) <= C_VERSION`（用户语义版本，默认 `X"FFFFFFFF"`，见 [hdl/fpga_base_v1_0.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L31)）。
- `C_USE_INFO_FROM_SCRIPT=true` ⇒ `reg_rdata(0) <= BuildGitHash_c`（脚本注入的 8 位哈希，来自 `fpga_base_scripted_info_pkg`，顶层通过 [hdl/fpga_base_v1_0.vhd:25](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L25) 的 `use work.fpga_base_scripted_info_pkg.all` 引入）。

**第二处切换：固件日期组件的来源**。[hdl/fpga_base_v1_0.vhd:241-264](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241-L264)：

```vhdl
fpga_base_date_inst: entity work.fpga_base_date
generic map (
   C_DATE_YEAR           => BuildYear_c,
   C_DATE_MONTH          => BuildMonth_c,
   C_DATE_DAY            => BuildDay_c,
   C_DATE_HOUR           => BuildHour_c,
   C_DATE_MINUTE         => BuildMinute_c,
   C_USE_GENERIC_DATE    => C_USE_INFO_FROM_SCRIPT   -- 关键：总闸直连日期组件的子开关
)
port map (
   ...
   o_year                => reg_rdata( 1),   -- 0x04
   o_month               => reg_rdata( 2),   -- 0x08
   ...
);
```

注意 `C_DATE_*` 这五个泛型**总是**接 `BuildYear_c` 等（来自脚本化包），而**是否使用它们**则由日期组件内部的 `C_USE_GENERIC_DATE` 决定，而这个子开关被顶层直接连到了总闸 `C_USE_INFO_FROM_SCRIPT`：

- `true` ⇒ 日期组件走 `g_generics` 分支（[hdl/fpga_base_date_package.vhd:187-194](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L194)），用 `to_unsigned(C_DATE_YEAR, 32)` 把脚本注入的常量转成日期输出——**纯常量，综合期就定死，不依赖任何 FDPE/INIT**。
- `false` ⇒ 走 `g_ngenerics` 分支，从 FDPE 的 Q 读，由 `fpga_base.tcl` 在综合后写 `INIT`（u3-l1/u3-l2 已详述）。

于是**一个总闸同时拨动了版本号与日期**，二者必然同源，避免了「版本号说是 A 提交、日期却是 B 时间编译」的矛盾。

> 补充说明：在脚本化模式（`true`）下，日期走 `g_generics` 常量分支，FDPE 那条链路不再被日期输出使用；而 `fpga_base.tcl` 钩子在脚本化模式下也无需运行。这正是 u3-l1 结尾所说「两条注入路径互斥」在顶层的具体接线。

#### 4.3.4 代码实践

1. **实践目标**：在源码层面完整追踪「勾选 `C_USE_INFO_FROM_SCRIPT=true` 后，版本号与日期分别从哪里来」，体会单开关统管两条链路的一致性。
2. **操作步骤**：
   - 在 [hdl/fpga_base_v1_0.vhd:233-234](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L233-L234) 找到版本号的多路选择，写下 `true` 时 `reg_rdata(0)` 的驱动源。
   - 在 [hdl/fpga_base_v1_0.vhd:248](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L248) 找到 `C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT`，再跳到 [hdl/fpga_base_date_package.vhd:187-203](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L203) 确认 `true` 时走哪个 `generate` 分支、数据来自哪。
   - 把 [fpga_base.tcl:46-68](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L46-L68) 与 [scripts/update_version.py:74-84](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L74-L84) 放在一起对照。
3. **需要观察的现象**：确认 `C_USE_INFO_FROM_SCRIPT` 这个布尔值在顶层**出现两次**——一次喂版本号多路选择、一次喂日期组件的 `C_USE_GENERIC_DATE`——而没有第二个独立开关。
4. **预期结果**：
   - `true` 时：版本号 `0x00` = `BuildGitHash_c`；日期 `0x04~0x14` 来自 `BuildYear_c`…`BuildMinute_c`（经 `g_generics` 的 `to_unsigned`），二者**都由 `update_version.py` 注入**。
   - `false` 时：版本号 `0x00` = `C_VERSION`；日期来自 FDPE 的 INIT（由 `fpga_base.tcl` 写），二者**都不经过 Python 脚本**。
   - 单一开关 ⇒ 两条链路必然一致，不会出现混合状态。
5. 待本地验证：在 Vivado 中分别勾选/取消该参数，综合后用 u5-l3 将讲的 JTAG-to-AXI 调试脚本读 `0x00` 与 `0x04`，对比两次读数差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `C_USE_INFO_FROM_SCRIPT` 要同时控制版本号和日期，而不是拆成两个独立开关？

> **答案**：为了让两条链路**强一致**。版本号（哈希）和日期都是「构建身份」的一部分，必须来自同一次构建。如果拆成两个开关，就可能出现「版本号用了脚本注入的新哈希、日期却还是上次 TCL 钩子写的旧时间」的错位，反而误导运维。一个总闸从根上杜绝了这种不一致。

**练习 2**：在脚本化模式下，`fpga_base.tcl` 那个综合后写 `INIT` 的钩子还需要运行吗？为什么？

> **答案**：不需要。脚本化模式下日期走 `g_generics` 常量分支，直接用 `to_unsigned(C_DATE_*, 32)` 在综合期定死，根本不读 FDPE 的 Q。既然日期不依赖 FDPE 的 `INIT`，`fpga_base.tcl` 逐位写 `INIT` 的操作就失去了意义。这正是两条路径互斥的体现：选了「综合前改源码」，就不必再「综合后改网表」。

---

## 5. 综合实践

把三个最小模块串起来，做一次**纸面端到端注入演练**（不需要 Vivado，目的是验证你真的看懂了整条链路）。

**场景设定**：

- 消费方工程当前 git HEAD 的完整 SHA = `9b249a7a48c9f50f411c936da435eb5438aeb097`，工作区**干净**。
- 现在是 2026 年 7 月 20 日 14 时 37 分。
- 顶层勾选了 `C_USE_INFO_FROM_SCRIPT = true`。

**任务**：

1. **走 4.2（gitpython）**：算出 `commitHash8` 的值。仓库干净，所以直接取前 8 位，应得到 `9b249a7a`。
2. **走 4.1（占位符替换）**：参照 [hdl/fpga_base_scripted_info_pkg.vhd:12-17](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_scripted_info_pkg.vhd#L12-L17)，写出脚本运行后这 6 个常量的**新值**。预期：
   - `BuildYear_c := 2026;`、`BuildMonth_c := 7;`、`BuildDay_c := 20;`、`BuildHour_c := 14;`、`BuildMinute_c := 37;`
   - `BuildGitHash_c := X"9b249a7a";`
3. **走 4.3（双信息源切换）**：因为 `C_USE_INFO_FROM_SCRIPT=true`，追踪到顶层：
   - 版本寄存器 `0x00`（[hdl/fpga_base_v1_0.vhd:233-234](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L233-L234)）= `BuildGitHash_c` = `0x9b249a7a`。
   - 固件日期寄存器走 `g_generics` 分支，`0x04`（年）= `to_unsigned(2026, 32)` = `0x000007EA`、`0x08`（月）= `0x00000007`、`0x0C`（日）= `0x00000014`、`0x10`（时）= `0x0000000E`、`0x14`（分）= `0x00000025`。
4. **反思假设**：如果场景改成「工作区有未提交改动」（脏），第 1 步的 `commitHash8` 会变成什么？（答：`FFFFFFFF`，于是 `0x00` 读到 `0xFFFFFFFF`，运维一眼看出是不可复现构建。）

> 待本地验证：装好 gitpython 后，在一份干净测试仓库里真的跑一次 `python scripts/update_version.py`，再 `cat hdl/fpga_base_scripted_info_pkg.vhd` 比对注入结果；之后用 `git ls-files -v | grep '^h'` 确认该 HDL 文件被标记为 assume-unchanged。

---

## 6. 本讲小结

- fpga_base 用**「带 `$$tag$$` 注释的合法 VHDL + Python 正则替换」**实现了就地版本注入：模板本身可独立综合，tag 负责「定位行」，正则负责「改值」，二者解耦、可重复执行。
- `update_version.py` 用 **gitpython** 从**消费方工程仓库**取 8 位 git 哈希写入版本寄存器，使现场固件可追溯到具体的工程提交；取编译机当前时间写入日期常量。
- 对**脏仓库**（`is_dirty`）采用「警告 + 二次确认 + 哨兵值 `FFFFFFFF`」策略：不硬性阻止，但抹掉哈希可信度，读到全 `F` 即知是不可复现构建。
- 结尾的 `git update-index --assume-unchanged` 把脚本就地改写的 HDL 文件**对 git 隐藏**，保证库内提交版本始终是干净的占位模板，避免误提交和长期「脏」状态。
- 顶层 `C_USE_INFO_FROM_SCRIPT` 是**单一总闸**，同时切换「版本号寄存器来源」和「日期组件来源」，强制两条链路同源一致。
- 该**脚本化 Python 路径**与 u3-l2 的**传统 TCL 综合钩子路径**互斥：前者综合前改源码常量、提供哈希+日期；后者综合后改 FDPE 网表、只管日期配用户语义版本号。

---

## 7. 下一步学习建议

本讲讲完了「构建信息如何进入硬件」的全部两条路径，接下来建议：

1. **向上回到工程化**：进入第 4 单元，学习 `scripts/package.tcl` 如何用 PsiIpPackage 把这些 HDL（包括 `fpga_base_scripted_info_pkg.vhd`）连同 `C_USE_INFO_FROM_SCRIPT` 参数打包成可分发 IP（见 [scripts/package.tcl:27](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L27) 与 [scripts/package.tcl:65](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L65)），理解 `component.xml` 里 `MODELPARAM_VALUE.C_USE_INFO_FROM_SCRIPT` 与 `PARAM_VALUE.C_USE_INFO_FROM_SCRIPT` 的区别。
2. **向下进入软件栈**：进入第 5 单元 u5-l1，看裸机 C 驱动如何读出本讲注入的版本号与固件日期，并与软件自身的 `__DATE__/__TIME__` 对照，判断软硬配套。
3. **延伸阅读**：对比 `fpga_base.tcl`（[fpga_base.tcl:46-144](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L46-L144)）与 `update_version.py`（[scripts/update_version.py:34-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/update_version.py#L34-L89)），体会「综合后改网表」与「综合前改源码」两种构建信息注入范式各自的工程权衡。
