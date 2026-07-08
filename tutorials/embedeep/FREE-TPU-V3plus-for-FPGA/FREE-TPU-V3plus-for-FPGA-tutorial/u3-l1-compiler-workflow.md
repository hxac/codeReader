# eeptpu_compiler 编译工作流与命令行参数

## 1. 本讲目标

本讲是「模型编译链路」单元的第一讲。学完之后，你应该能够：

- 说清楚 `eeptpu_compiler` 的**输入**（darknet 的 cfg/weights、校准图像）和**输出**（`*.pub.bin`）分别是什么。
- 读懂 `setting.ini` 里 `[Setting]` 段四个键的含义，并能解释其中 6 个被注释掉的编译方案在**量化、多线程、仿真数据**上的差异。
- 逐段讲清 `b_yolo4tiny.sh` 是怎么读 ini、拼命令、跑编译、搬运产物的。
- 理解 `--public_bin`、`--hybp`、`--base_*`、`--mean/--norm`、`--int8`、`--tpu_threads`、`--sim_data` 等关键参数各自控制了 bin 的什么属性。

本讲只聚焦「编译器这一步」：框架模型如何变成 TPU 可执行的 bin。把 bin 进一步转成裸机可用的 `eepnet.h`/`eepnet.mem` 是下一讲（u3-l2）的内容。

## 2. 前置知识

在进入源码前，先用三段话把背景补齐（承接 u1/u2 已建立的认识）：

- **TPU bin 是什么**：EEP-TPU V3+ 是一块硬件，它不直接吃 Pytorch/Caffe/Darknet 的模型文件，只执行自己专属的二进制（`.pub.bin`）。这个 bin 不仅仅是一堆权重，它还打包了**算子调度表、张量地址表、输入输出 shape、mean/norm 等预处理参数**——可以理解为「给 TPU 的一整套可执行程序 + 数据布局说明书」。`eeptpu_compiler` 就是把这个 bin 产出来的工具。
- **编译器跑在哪里**：编译是在**开发主机（x86 PC）**上完成的，不在 ARM 板上。下文会看到 `compiler/eeptpu_compiler` 是一个 x86-64 的 ELF 可执行文件；编译产物再拿到板子上运行。
- **本讲的两个文件**：编译过程被组织成「一个配置文件 `setting.ini` + 一个驱动脚本 `b_yolo4tiny.sh`」。配置文件负责「编什么、怎么编」，脚本负责「读配置 → 拼命令行 → 调编译器 → 收拾产物」。这种把**参数**和**流程**分离的写法，是这个项目所有编译脚本的通用范式。

> 名词速查：**cfg** 是 darknet 的网络结构文本描述；**weights** 是训练好的权重；**量化（quantization）** 把浮点权重/激活压成 INT8 以提速省存；**校准（calibration）** 用少量真实样本确定量化时的缩放因子。

## 3. 本讲源码地图

本讲只涉及 `sdk/standalone/net_model/` 下的少量文件，它们构成「编译一个网络」的最小闭环：

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `sdk/standalone/net_model/scripts/setting.ini` | 编译配置：指定编译器路径、模型根目录、全局命令行参数、产物 bin 名 | 是（核心） |
| `sdk/standalone/net_model/scripts/b_yolo4tiny.sh` | 编译驱动脚本：读 ini、拼命令、调编译器、搬运产物 | 是（核心） |
| `sdk/standalone/net_model/compiler/eeptpu_compiler` | 编译器本体，x86-64 闭源二进制 | 只用，不读 |
| `sdk/standalone/net_model/models/yolov4tiny/` | 输入模型：`yolov4_tiny.cfg` + `yolov4_tiny.weights` | 作为输入提及 |
| `sdk/standalone/src/config.h` | 裸机运行时配置（含基地址），用于和编译期基地址做对照 | 对照引用 |
| `doc/eep-ug050 ... Compiler User Manual_230201.pdf` | 编译器官方手册，参数权威定义 | 推荐对照 |

## 4. 核心概念与源码讲解

### 4.1 setting.ini 配置项

#### 4.1.1 概念说明

`setting.ini` 是一个标准的 **INI 配置文件**：用一个 `[Setting]` 段把编译所需的四个关键信息集中起来。它的设计思想是「**把会变的东西放进 ini，把不变的流程放进 sh**」：

- `compiler`：编译器可执行文件的相对路径。
- `model_root`：模型与图像资源的根目录。
- `global_cmd`：传给编译器的**全局命令行参数**（决定精度、线程、基地址、是否出仿真数据等）。
- `bin_name`：编译产物的文件名。

最关键的是 `global_cmd`：项目在这里**预置了 6 套不同的编译方案**，其中 5 套被注释掉，只启用 1 套。切方案的方式就是「注释掉一行、启用另一行」——不需要改脚本。这正是本讲实践任务要分析的内容。

#### 4.1.2 核心流程

`setting.ini` 本身只是静态文本，它的消费流程是：

1. `b_yolo4tiny.sh` 启动后，用内置的 `read_ini()` 函数读取 `[Setting]` 段的四个键。
2. 把读到的值拼进编译器命令行。
3. 注释（`#`）在 INI 里没有特殊语义，`read_ini` 只会取到**第一个未被注释、且键名匹配**的值——所以同时只能有一行 `global_cmd=...` 生效。

#### 4.1.3 源码精读

先看整个文件的全貌（[setting.ini:L1-L10](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L1-L10)）：

```ini
[Setting]
compiler=../compiler/eeptpu_compiler
model_root=../models/
#==============================================================================
# s2+sim
global_cmd=--public_bin --hybp --base_par 0x30000000 --base_in 0x30000000 --base_out 0x30000000 --base_tmp 0x80000000
bin_name=eeptpu_s2.pub.bin
```

中文说明：

- `compiler=../compiler/eeptpu_compiler`：编译器是 scripts 目录的上一级里的 `compiler/eeptpu_compiler`。注意是**相对路径**，所以脚本必须先 `cd` 进 scripts 目录才能找到它（这一点在 4.2 会看到）。
- `model_root=../models/`：模型与图像都放在上一级的 `models/` 下。
- 第 8–10 行是**当前启用的方案**，标签写着 `# s2+sim`：
  - `global_cmd` 里只有 `--public_bin --hybp` 加四个 `--base_*` 基地址，**没有** `--int8`、**没有** `--tpu_threads`、**也没有** `--sim_data`。
  - `bin_name=eeptpu_s2.pub.bin` 是产物名。

再看被注释掉的另外 5 套方案（[setting.ini:L12-L31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L12-L31)），它们才是理解参数差异的关键：

```ini
# s2quant+sim
#global_cmd=--public_bin --hybp --sim_data --base_par 0x30000000 ... --int8
#bin_name=nntpu_int8.pub.bin

# s2t4+sim
#global_cmd=--public_bin --hybp --tpu_threads 4 --sim_data --base_par 0x30000000 ...
#bin_name=nntpu_s2.pub.bin

# s2t4quant+sim
#global_cmd=--public_bin --hybp --tpu_threads 4 --sim_data --base_par 0x30000000 ... --int8
#bin_name=nntpu_int8.pub.bin

# s2t2+sim
#global_cmd=--public_bin --hybp --tpu_threads 2 --sim_data --base_par 0x30000000 ...
#bin_name=nntpu_s2.pub.bin

# s2t2 no_sim
#global_cmd=--public_bin --hybp --tpu_threads 2
#bin_name=nntpu_s2.pub.bin
```

> ⚠️ **阅读时要「以命令行实际内容为准」，别被注释标签带偏**：当前启用的一行标签写的是 `s2+sim`，但它**实际并没有 `--sim_data`**；而其余带 `+sim` 的方案都显式带了 `--sim_data`。这是仓库自身注释与命令行的一处不一致，阅读源码时应相信 `global_cmd=` 后面的真实内容，而不是上面的 `#` 标签。

#### 4.1.4 代码实践

> **实践目标**：把 `setting.ini` 里 6 套方案在「量化 / 线程数 / 仿真数据」三个维度上的差异整理成表，并指出当前启用的是哪一套。
>
> **操作步骤**：
> 1. 打开 `sdk/standalone/net_model/scripts/setting.ini`。
> 2. 对每一行非注释/被注释的 `global_cmd=`，检查三个 flag 是否存在：`--int8`（量化）、`--tpu_threads N`（线程数）、`--sim_data`（仿真数据）。
>
> **需要观察的现象 / 预期结果**：按下表逐行核对（以**实际 flag** 为准）：

| 方案标签 | 行号 | `--int8` | `--tpu_threads` | `--sim_data` | 精度 | 产物名 | 是否启用 |
|----------|------|----------|-----------------|--------------|------|--------|----------|
| `s2+sim` | L8–L10 | 无 | 无（默认） | **无** | FP16 | `eeptpu_s2.pub.bin` | ✅ 当前启用 |
| `s2quant+sim` | L12–L14 | 有 | 无（默认） | 有 | INT8 | `nntpu_int8.pub.bin` | ❌ |
| `s2t4+sim` | L16–L18 | 无 | 4 | 有 | FP16 | `nntpu_s2.pub.bin` | ❌ |
| `s2t4quant+sim` | L20–L22 | 有 | 4 | 有 | INT8 | `nntpu_int8.pub.bin` | ❌ |
| `s2t2+sim` | L25–L27 | 无 | 2 | 有 | FP16 | `nntpu_s2.pub.bin` | ❌ |
| `s2t2 no_sim` | L29–L31 | 无 | 2 | 无 | FP16 | `nntpu_s2.pub.bin` | ❌ |

**结论**：当前启用的是 `s2+sim`（L8–L10），即**默认 FP16、默认线程数、且不生成仿真数据**的最简方案。

> 关于命名：`quant` 后缀 = 带 `--int8`（量化）；`t4`/`t2` = `--tpu_threads 4/2`（多线程调度）；`+sim` = 带 `--sim_data`（出仿真数据）。`s2` 是项目对「基础单核 bin 格式」的内部标签，其在编译器里的精确定义需查 `doc/eep-ug050` 手册，脚本本身未注释，**待确认**。

#### 4.1.5 小练习与答案

**练习 1**：如果想把网络编成 INT8 版本，最少要改 `setting.ini` 的哪些地方？
**答**：把当前 L8–L10 注释掉，启用 L12–L14（`s2quant+sim`）或 L20–L22（`s2t4quant+sim`），关键是 `global_cmd` 里要带 `--int8`；同时 `bin_name` 会变成 `nntpu_int8.pub.bin`，下游引用该 bin 名的地方（如 u3-l2 的 `eepbin_cvt.sh`）要同步改名。

**练习 2**：为什么同时只能有一行 `global_cmd=` 生效？
**答**：因为 `b_yolo4tiny.sh` 的 `read_ini()` 只返回**第一个**键名匹配且未被注释的值（详见 4.2.3 的 awk 逻辑，匹配后会 `a=0` 立即停止）。多行未注释会导致取到非预期的那一行。

---

### 4.2 b_yolo4tiny.sh 编译流程

#### 4.2.1 概念说明

如果说 `setting.ini` 是「参数表」，那 `b_yolo4tiny.sh` 就是「执行引擎」。它做四件事：

1. **定位自己**：`cd` 到脚本所在目录，让 `setting.ini` 里的相对路径（`../compiler`、`../models`）能正确解析。
2. **读配置**：用一个小型 `read_ini` 函数从 ini 里取四个键。
3. **拼命令并执行**：把 `global_cmd`、模型路径、mean/norm、校准图像等拼成一条完整的编译器命令行，`eval` 执行。
4. **收拾产物**：编译成功后，把生成的 bin 和（若有）仿真数据搬到指定的输出目录。

它还支持三个位置参数（网络名、bin 目录、仿真目录），但要注意一个**重要陷阱**（见 4.2.4）。

#### 4.2.2 核心流程

用伪代码描述整个脚本主干：

```
cd 脚本所在目录                       # 让 ../相对路径生效
compiler   = read_ini(setting.ini, compiler)
model_root = read_ini(setting.ini, model_root)
global_cmd = read_ini(setting.ini, global_cmd)
bin_name   = read_ini(setting.ini, bin_name)

# 可选位置参数覆盖默认值
netName = $1 ?? "yolov4tiny"
binDir  = $2 ?? "binRoot"
simDir  = $3

mkdir -p binDir/netName, simDir/netName      # 清理并重建输出目录

cfg = model_root + "yolov4tiny/yolov4_tiny.cfg"     # ⚠ 路径硬编码
wts = model_root + "yolov4tiny/yolov4_tiny.weights" # ⚠ 路径硬编码

cmd = compiler + global_cmd
    + --output ./ --mean ... --norm ...
    + --darknet_cfg cfg --darknet_weight wts
    + --image 校准图 --extinfo 'classes=...' --input_folder 校准图目录

eval $cmd                                     # 真正调编译器

if 成功:
    把 ./output/*  → simDir/netName          # 搬仿真数据
    把 bin_name    → binDir/netName          # 搬 bin
else:
    退出
```

#### 4.2.3 源码精读

**(a) 先定位自己**（[b_yolo4tiny.sh:L1-L4](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L1-L4)）：

```bash
basepath=$(cd `dirname $0`; pwd)
basepath=${basepath}"/"
cd ${basepath}
```

中文说明：取脚本自身所在目录的绝对路径并 `cd` 进去。这一步是 `setting.ini` 里所有 `../` 相对路径能找到文件的前提——如果不 cd，从别的目录调用脚本就会找不到编译器和模型。

**(b) 内置的 INI 解析器** `read_ini`（[b_yolo4tiny.sh:L27-L31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L27-L31)）：

```bash
function read_ini() {
    file=$1;section=$2;item=$3;
    val=$(awk -F '=' '/\['${section}'\]/{a=1} (a==1 && "'${item}'"==$1){a=0;print $2}' ${file})
    echo ${val}
}
```

中文说明：这是一行精巧的 `awk` 迷你解析器：

- `-F '='`：以等号为分隔符，`$1` 是键、`$2` 是值。
- `/\[Setting\]/{a=1}`：遇到 `[Setting]` 段头时，把标志位 `a` 置 1（进入该段）。
- `(a==1 && "compiler"==$1){a=0;print $2}`：在段内，若键名匹配要找的项，就打印它的值，并立刻把 `a` 置 0（只取第一个匹配）。
- 被注释的行（`#global_cmd=...`）在 awk 眼里整行就是一个键名为 `#global_cmd` 的项，与目标键名 `global_cmd` 不相等，所以天然被跳过。

随后脚本用它取出四个键并做非空校验（[b_yolo4tiny.sh:L33-L45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L33-L45)）：`compiler`、`model_root` 没设置就直接退出。

**(c) 构造并执行编译命令**（[b_yolo4tiny.sh:L90-L100](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L90-L100)）：

```bash
cfg=${model_root}"yolov4tiny/yolov4_tiny.cfg"
wts=${model_root}"yolov4tiny/yolov4_tiny.weights"
img_ssd=${model_root}"/images/004545.bmp"
input_dir="--input_folder "${model_root}"images/ssd/"
extinfo=" --extinfo 'classes=background,person,...,toothbrush' "

cmd="${compiler} ${global_cmd} --output ./ --mean '0.0,0.0,0.0' --norm '0.003921569,0.003921569,0.003921569' --darknet_cfg ${cfg} --darknet_weight ${wts} --image ${img_ssd} ${extinfo} ${input_dir}"
echo ${cmd}
eval $cmd
```

中文说明：

- `cfg`/`wts`：darknet 的结构文件和权重文件（输入模型）。
- `img_ssd` / `input_dir`：一张校准图和一个校准图目录——量化或出仿真数据时需要真实样本。
- `cmd` 把 `global_cmd`（来自 ini）和脚本里固定的 `--mean/--norm/--darknet_cfg/...` 拼成完整命令，`echo` 打印后用 `eval` 执行。各参数含义在 4.3 统一解读。

**(d) 收拾产物**（[b_yolo4tiny.sh:L102-L119](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L102-L119)）：

```bash
if [ $? == 0 ]; then
    if [ -d ${simDir}/${netName} ]; then
        ... mv ./output/* ${simDir}/${netName}    # 搬仿真数据
    fi
    if [ -d ${binDir}/${netName} ]; then
        ... mv ./${bin_name} ${binDir}/${netName}  # 搬 bin
    fi
fi
```

中文说明：`$?` 是上一条命令（编译器）的退出码，为 0 才算成功。成功后，若指定了 `simDir`，就把编译器吐在 `./output/` 里的仿真数据搬过去；若指定了 `binDir`，就把 bin 搬进 `binDir/netName/`。

#### 4.2.4 代码实践

> **实践目标**：搞清楚「换一个网络名」到底会不会换编译的模型，并定位硬编码点。
>
> **操作步骤**：
> 1. 阅读脚本头部的用法注释（[b_yolo4tiny.sh:L6-L12](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L6-L12)），确认它声称支持 `bash b_yolo4tiny.sh netName [binDir] [simDir]`。
> 2. 再看 L90–L91，注意 `cfg`/`wts` 的路径里写死的是 `yolov4tiny/`。
>
> **需要观察的现象 / 预期结果**：
> - `netName`（无论默认 `yolov4tiny` 还是用 `$1` 覆盖）**只影响输出目录名**（`binDir/netName`、`simDir/netName`），**不影响实际编译的模型**。
> - 因为 L90–L91 把模型路径硬编码成了 `yolov4tiny/yolov4_tiny.cfg` 与 `yolov4tiny/yolov4_tiny.weights`。
>
> **结论（也是踩坑提醒）**：要编译**别的网络**（比如 mobilenet-ssd），光传 `netName` 参数没用，必须**手动改 L90/L91 的 cfg/wts 路径**，并视情况改 L95 的 `--extinfo` 类别表。本脚本本质上是「yolov4-tiny 专用」的，`netName` 参数主要用来给产物归档分目录。

> 说明：本实践为**源码阅读型实践**，无需运行编译器；若要在主机上实跑，需自备 x86-64 Linux 环境并确认 `compiler/eeptpu_compiler` 可执行，运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`read_ini` 为什么能自动跳过被 `#` 注释的行？
**答**：被注释的行整体形如 `#global_cmd=...`，以 `=` 分隔后键名是 `#global_cmd`，与目标键名 `global_cmd` 字符串不等，所以 awk 的 `"global_cmd"==$1` 判断为假，不会被打印。

**练习 2**：如果不执行开头的 `cd ${basepath}`，最先会在哪一步出错？
**答**：会在读取完 ini 后、执行编译器时出错——因为 `compiler=../compiler/eeptpu_compiler` 是相对路径，不先 cd 到 scripts 目录就找不到该可执行文件（或会误解析到当前工作目录的相对路径）。

---

### 4.3 关键命令行参数解读

#### 4.3.1 概念说明

编译器的命令行参数虽然多，但可以归成五类，记住这五类就不会乱：

| 类别 | 代表参数 | 控制什么 |
|------|----------|----------|
| 产物格式 | `--public_bin`、`--hybp` | 输出哪种 bin、怎么打包 |
| 地址布局 | `--base_par/--base_in/--base_out/--base_tmp` | 张量在 DDR 的基地址 |
| 预处理 | `--mean`、`--norm` | 像素如何归一化（写进 bin） |
| 输入模型 | `--darknet_cfg`、`--darknet_weight`、`--extinfo` | 网络结构、权重、类别元数据 |
| 性能/校准 | `--int8`、`--tpu_threads`、`--sim_data`、`--image`、`--input_folder` | 精度、线程、量化校准样本、仿真数据 |

需要特别强调：**`--mean` 和 `--norm` 是被烤进 bin 的**。也就是说，预处理参数在编译期就固定了，运行时喂给 TPU 的是「原始像素」，归一化由 TPU 按编译期写死的系数来做（裸机路线则在 `get_input_data` 里自己做等价处理，见 u4-l4）。

#### 4.3.2 核心流程

**预处理数学**：脚本里 `--mean '0.0,0.0,0.0'`、`--norm '0.003921569,0.003921569,0.003921569'`，对每个像素的每个通道做：

\[
x_{\text{norm}} = (x - \text{mean}) \times \text{norm}
\]

代入数值（注意 \(0.003921569 \approx \dfrac{1}{255}\)）：

\[
x_{\text{norm}} = (x - 0) \times \frac{1}{255} = \frac{x}{255}
\]

这正是 darknet/yolo 系列最标准的归一化：把 \([0,255]\) 的像素映射到 \([0,1]\)。三个通道值相同，表示三通道等比缩放、不偏移。

**INT8 量化的直觉**（仅当加 `--int8` 时）：编译器用 `--image`/`--input_folder` 提供的校准样本跑一遍网络，统计各层激活的数值范围，算出每层的缩放因子 \(s\)，再把浮点权重和激活压成 8 位整数：

\[
x_{\text{int8}} = \operatorname{round}\!\left(\frac{x_{\text{fp}}}{s}\right), \qquad s > 0
\]

这样推理时用整数运算，速度更快、显存更省，代价是微小的精度损失。具体的量化算法（对称/非对称、per-tensor/per-channel）以 `doc/eep-ug050` 手册为准。

#### 4.3.3 源码精读

完整命令行在 [b_yolo4tiny.sh:L97](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L97)（结构化拆开看）：

```bash
${compiler} ${global_cmd} \
  --output ./ \
  --mean '0.0,0.0,0.0' --norm '0.003921569,0.003921569,0.003921569' \
  --darknet_cfg  ${cfg} --darknet_weight ${wts} \
  --image ${img_ssd} ${extinfo} ${input_dir}
```

逐个解读（`global_cmd` 部分来自 [setting.ini:L9](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L9)）：

- `--public_bin`：产出「公开版（Free TPU）」bin 格式，与商用加密 bin 区分。**这个 flag 是免费评估版的身份标志**。
- `--hybp`：与 `--public_bin` 配对使用的打包模式，让产物成为一个自包含的混合 bin（权重 + 调度表 + 地址表 + IO shape 打包在一起）。两者在本项目里**总是成对出现**。字节级的精确定义见 ug050 手册。
- `--base_par 0x30000000` / `--base_in 0x30000000` / `--base_out 0x30000000`：分别为**参数(权重)、输入张量、输出张量**指定编译期假定的 DDR 基地址。
- `--base_tmp 0x80000000`：临时/中间缓冲（scratch）的基地址。
- `--mean` / `--norm`：见 4.3.2，预处理系数，烤进 bin。
- `--darknet_cfg` / `--darknet_weight`：darknet 框架的模型结构与权重（编译器还支持 caffe/onnx/ncnn/keras，对应不同的 `--xxx_cfg` 参数）。
- `--image` / `--input_folder`：校准/仿真用的样本图像（量化或出仿真数据时必需）。
- `--extinfo 'classes=...'`：把人类可读的**类别名表**作为元数据塞进 bin，供后处理把「类别索引」翻译成「类别名字」。本例里是一份以 `background` 开头的检测类别清单。
- `--output ./`：把产物吐到当前目录（scripts/）。

> **关于基地址的一个关键观察**：编译期 `--base_*` 用的是 `0x30000000`（输入/输出/参数）和 `0x80000000`（临时），而裸机运行时 [config.h:L25-L26](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L26) 里的 `EEPTPU_MEM_BASE_ADDR` 却是 `0x31000000`。两者并不冲突也不可随便改：`--base_*` 告诉编译器「假设张量位于哪个 DDR 地址」以便在 bin 内编排地址表；而运行时实际把网络/张量搬到哪个物理地址、`BASEADDR` 寄存器里填什么值，由下一讲的 `eepBinCvt` 转换和 u4 的寄存器协议共同决定。**编译期地址与运行期地址如何对齐，是贯穿 u3–u5 的主线，本讲先记住「编译器写死了一组基地址进 bin」即可。**

#### 4.3.4 代码实践

> **实践目标**：通过修改 `--mean/--norm` 理解「预处理被烤进 bin」的含义，并能预测改动的后果。
>
> **操作步骤**：
> 1. 在 [b_yolo4tiny.sh:L97](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L97) 把 `--norm` 从 `0.003921569` 改成 `0.007843138`（约 \(2/255\)）。
> 2. 重新编译（或仅在脑中推演）。
>
> **需要观察的现象 / 预期结果**：
> - 归一化系数翻倍 → 喂给网络的输入整体放大 2 倍。
> - 由于 yolo 是用 `norm=1/255` 训练的，输入分布偏移会导致检测置信度/框回归明显变差，甚至检不出目标。
> - 这反过来证明：**你不需要在运行时手动做 `/255`，因为编译器已经把这个系数固化进了 bin**（裸机侧的 `get_input_data` 做的是与之匹配的定点化，而不是另算一套归一化，详见 u4-l4）。
>
> 说明：本实践若不实际编译，可改为「阅读型」——只需解释清楚改动的影响即可，运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`--mean '0.0,0.0,0.0'` 三个 `0.0` 分别对应什么？如果想做 ImageNet 风格的按通道减均值（如 `123.675,116.28,103.53`），该怎么改？
**答**：三个值分别对应 R/G/B 三个通道的均值。改成 `--mean '123.675,116.28,103.53'` 即可（同时要保证 `--norm` 与训练时的预处理一致，否则分布不匹配）。

**练习 2**：`--public_bin` 和 `--hybp` 能不能只写其中一个？
**答**：在本项目的全部 6 套方案里，二者总是**成对出现**。`--public_bin` 决定「免费公开版格式」，`--hybp` 决定「混合打包方式」；单独使用的语义需查 ug050 手册确认，本讲不建议拆开用。

---

### 4.4 输出产物组织

#### 4.4.1 概念说明

编译跑完后会产生两类东西：

1. **bin 文件**（必有）：即 `bin_name` 指定的 `*.pub.bin`，是 TPU 的「可执行程序」。
2. **仿真数据**（可选）：当 `global_cmd` 带 `--sim_data` 时，编译器会在主机上用校准图像跑一遍网络，把中间/输出张量 dump 到 `./output/` 目录，供后续和上板结果逐层比对。这是验证「编译有没有把精度搞坏」的重要手段。

脚本通过 `binDir` 和 `simDir` 这两个目录，把产物按网络名分门别类归档。

#### 4.4.2 核心流程

产物归档的目录结构如下（`netName` 默认 `yolov4tiny`）：

```
scripts/
├── setting.ini
├── b_yolo4tiny.sh
├── binRoot/                       ← binDir（默认）
│   └── yolov4tiny/                ← netName
│       └── eeptpu_s2.pub.bin      ← 编译产物（bin_name 决定）
├── <simDir>/                      ← 若传了第 3 个参数
│   └── yolov4tiny/
│       └── output 里搬来的仿真张量
└── output/                        ← 编译器临时吐仿真数据处，搬完即弃
```

只有当命令行传了第 2、第 3 个参数时，`binDir`/`simDir` 才会被启用（`simDir` 默认为空，不搬仿真数据）。

#### 4.4.3 源码精读

**(a) 输出目录的创建与清理**（[b_yolo4tiny.sh:L63-L87](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L63-L87)）：

```bash
if [ -n "${binDir}" ]; then
    if [ ! -d ${binDir}/${netName} ]; then mkdir ${binDir}/${netName}
    else rm -rf ${binDir}/${netName}; mkdir ${binDir}/${netName}    # 先删后建
    fi
fi
```

中文说明：若 `binDir` 非空，就确保 `binDir/netName` 存在；若已存在则**先删后建**，保证每次编译都是干净产物，不会残留旧文件。`simDir` 走完全相同的逻辑。

**(b) 产物的搬运**已在 4.2.3 (d) 引用（[b_yolo4tiny.sh:L102-L112](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L102-L112)）：成功后把 `./output/*`（仿真数据）搬到 `simDir/netName`，把 `bin_name` 搬到 `binDir/netName`。

**(c) bin 文件名的意义**：`bin_name` 由 `setting.ini` 决定（如 `eeptpu_s2.pub.bin` 或 `nntpu_int8.pub.bin`）。这个名字会被下游引用——下一讲的 `eepbin_cvt.sh` 里就**硬编码**了 `./scripts/binRoot/yolov4tiny/eeptpu_s2.pub.bin` 作为输入（见 [eepbin_cvt.sh:L3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3)）。所以**如果你改了 `bin_name`（比如换 INT8 方案），必须同步改 `eepbin_cvt.sh` 里的路径**，否则下一步转换会找不到 bin。

#### 4.4.4 代码实践

> **实践目标**：在不实际编译的前提下，理清「换一套编译方案」会引发的产物名与下游联动改动。
>
> **操作步骤**：
> 1. 假设你要从当前 `s2+sim`（产物 `eeptpu_s2.pub.bin`）切到 `s2quant+sim`（产物 `nntpu_int8.pub.bin`）。
> 2. 打开 `eepbin_cvt.sh` 看 L3、L8 两条命令引用的 bin 路径。
>
> **需要观察的现象 / 预期结果**：
> - 切方案后 `binRoot/yolov4tiny/` 下的文件会从 `eeptpu_s2.pub.bin` 变成 `nntpu_int8.pub.bin`。
> - `eepbin_cvt.sh` 里写死的 `eeptpu_s2.pub.bin` 路径会失效，必须改成 `nntpu_int8.pub.bin`，否则转换报「找不到文件」。
> - 这说明：**编译不是孤立的一步，bin 的名字和路径是编译→转换→裸机加载整条链路的契约**。
>
> 说明：产物实际长什么样「待本地验证」（需在 x86 主机跑通编译）。

#### 4.4.5 小练习与答案

**练习 1**：为什么脚本对输出目录采用「先 `rm -rf` 再 `mkdir`」而不是直接覆盖？
**答**：为了保证产物目录是干净的——避免上一次编译遗留的旧 bin/旧仿真数据混进本次结果，造成「用了旧模型」的隐蔽 bug。

**练习 2**：当前启用方案没有 `--sim_data`，那 `./output/` 还会有内容吗？
**答**：不会有仿真张量。脚本里搬运 `./output/*` 的逻辑也会因为目录不存在或为空而跳过（L104 有 `[ -d ./output ] && ls -A` 数量大于 0 的判断）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿性任务**：

> **任务**：假设产品需求要求「用 4 线程、FP16 跑 yolov4-tiny，并且要在主机上拿到仿真参考数据用于和上板结果比对」。请基于 `setting.ini` + `b_yolo4tiny.sh` 给出完整操作方案。

参考解答步骤：

1. **选方案**：对照 4.1.4 的表，「4 线程 + FP16 + 仿真数据」对应 `s2t4+sim`（[setting.ini:L16-L18](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/setting.ini#L16-L18)）。
2. **改 ini**：把当前 L9–L10 注释掉，启用 L17–L18（去掉行首 `#`）。注意 `bin_name` 会变成 `nntpu_s2.pub.bin`。
3. **跑脚本（带 simDir）**：因为要拿仿真数据，必须传第 3 个参数指定仿真目录，例如：
   ```bash
   cd sdk/standalone/net_model/scripts
   bash b_yolo4tiny.sh yolov4tiny binRoot simRoot
   ```
   不传 `simRoot` 的话，[b_yolo4tiny.sh:L103-L107](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L103-L107) 的搬运分支不会触发，仿真数据就只会留在 `./output/` 里。
4. **预期产物**：
   - `binRoot/yolov4tiny/nntpu_s2.pub.bin`（4 线程 FP16 的 bin）
   - `simRoot/yolov4tiny/`（仿真参考张量）
5. **联动改动**：因为 bin 名变了，下一步若要喂给裸机，需把 [eepbin_cvt.sh:L3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/eepbin_cvt.sh#L3) 里的 `eeptpu_s2.pub.bin` 同步改成 `nntpu_s2.pub.bin`。

> 这个任务把「读配置 → 选参数 → 跑脚本 → 理解产物 → 下游联动」整条链路走了一遍，是本讲核心。运行结果「待本地验证」（需 x86-64 Linux 主机与可执行的 `eeptpu_compiler`）。

## 6. 本讲小结

- **编译链路的入口**是 `b_yolo4tiny.sh` + `setting.ini` 这对组合：ini 管「编什么、怎么编」，脚本管「怎么跑、怎么归档」。
- **`setting.ini` 预置了 6 套方案**，靠注释/启用切换；当前启用的是 `s2+sim`（默认 FP16、默认线程、**无** `--sim_data`），尽管它的标签写着 `+sim`——要以 `global_cmd` 实际内容为准。
- **`b_yolo4tiny.sh`** 用一行 `awk` 写了个迷你 INI 解析器；先 `cd` 到脚本目录让相对路径生效；模型路径（`yolov4tiny/`）是**硬编码**的，换网络必须手改脚本。
- **参数分五类**：产物格式（`--public_bin --hybp`）、地址布局（`--base_*`）、预处理（`--mean 0 --norm 1/255`）、输入模型（`--darknet_cfg/weight --extinfo`）、性能/校准（`--int8 --tpu_threads --sim_data`）。
- **预处理系数被烤进 bin**：`--norm 0.003921569 ≈ 1/255` 实现 \(x/255\) 归一化，运行时无需重算。
- **bin 名是契约**：`bin_name` 一旦改变（如换 INT8），下游 `eepbin_cvt.sh` 的硬编码路径必须同步修改。

## 7. 下一步学习建议

- 下一讲 **u3-l2（eepBinCvt：把 bin 转成裸机可用的 mem/header）** 会接住本讲的产物 `*.pub.bin`，讲解 `eepBinCvt` 如何把它转成裸机工程 `#include` 的 `eepnet.h` / `eepnet.mem`，打通「编译 → 裸机部署」的最后一公里。
- 之后 **u3-l3（eepnet 配置数组格式解析）** 会深入 `eepnet.h` 里那个 `eepnet_config` 数组的二进制布局——你会看到本讲的 `--base_*`、`--mean/--norm`、输入输出 shape 是如何被序列化进数组、又被 `eeptpu_init` 读回来的。
- 建议同时翻阅 `doc/eep-ug050 EEP-TPU 编译器使用手册_230201.pdf`，对照确认本讲中标注「待确认」的参数（如 `--hybp`、`s2` 命名）的官方定义。
- 如果想从运行侧反推编译参数的意义，可先跳读 u4-l2（EEPTPU_SA 与寄存器协议）中 `BASEADDR` 寄存器的用法，理解编译期基地址与运行期寄存器的关系。
