# Yosys + slang 前端：SystemVerilog 到 RTLIL

## 1. 本讲目标

经过单元 3，我们把 PyTorch 模型一路降级成了 **SystemVerilog 文本**（一堆 `.sv` 文件加一份 `sources.f` 清单，由 `hw_clean_to_sv.sh` 产出）。但这些文本还只是「人能读、机器还没读」的源码。要真正做综合、算资源、出比特流，必须先有一个综合工具把这些 `.sv` 「读懂」并翻译成它自己的内部表示。

本讲就是这条降级链上 **Yosys 段的入口**。`sv_to_il.sh` 用带 `yosys-slang` 插件的 Yosys，以 `read_slang` 命令把 SystemVerilog 读进来，落地成 **RTLIL**（Yosys 的 RTL 中间表示）文件，交给后续综合。

学完本讲你应该能：

1. 说清「为什么用 yosys-slang 而不是 Yosys 自带的 `read_verilog`」。
2. 看懂 `write_yosys_slang_script` 如何把文件清单分类成「早 / 中 / 晚」三批，并理解两种读入模式（单次 elaborate vs 逐文件 extern）。
3. 看懂 `run_yosys_script` 如何用退出码 `137`/`9` 区分「正常失败」与「内存爆炸（OOM）」，并给出不同的处置建议。

## 2. 前置知识

- **RTLIL（RTL Intermediate Language）**：Yosys 内部统一的硬件表示，文本格式，后缀常为 `.il`。Yosys 的一切综合 pass（opt、memory、techmap、synth_xilinx……）都作用在 RTLIL 上。把 SV 变成 RTLIL，是任何综合流程的第一步。
- **综合前端（frontend）**：把外部硬件描述语言（Verilog / SystemVerilog / VHDL）解析成 RTLIL 的那部分。Yosys 自带 `read_verilog`，本讲用的是第三方插件 `read_slang`。
- **elaborate（例化展开）**：从顶层模块出发，递归地把每个被实例化的子模块展开，得到完整设计层次。slang 是一个独立的工业级 SystemVerilog 编译前端（做 lint/编译），`yosys-slang` 把它包成 Yosys 插件。
- **blackbox / extern**：一个只有端口、没有内部实现的模块。综合时把它当成「外部黑盒」保留，不展开内部。本讲的 `--extern-modules` 就是把「找不到定义的模块」临时当 blackbox。
- **OOM（Out Of Memory）**：进程占用内存超过系统限制，被内核的 OOM killer 用 `SIGKILL`（信号 9）杀掉，进程退出码变成 \(137 = 128 + 9\)。这正是 TinyStories-1M 这种 4200 万 LUT 级设计在本站最常见的失败方式。
- **`set -euo pipefail`**：bash 严格模式。`-e` 表示任意命令失败立刻退出脚本。本讲的 `run_yosys_script` 要在 Yosys 失败时「先抓住退出码再决定怎么报错」，因此需要临时关掉 `-e`。

承接上一讲 [u3-l4 HW 到 SystemVerilog 导出](u3-l4-hw-to-systemverilog-export.md)：那一站的 `--export-split-verilog` 把每个 `hw.module` 拆成独立 `.sv`，并生成顶层 `main.sv` 与文件清单 `sources.f`。**本站 `sv_to_il.sh` 正是按这份 `sources.f` 逐文件把 SV 读进 Yosys。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/pipeline/sv_to_il.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh) | 本站主脚本：编排「加载 slang 插件 → 读 SV → hierarchy → stat → 写 RTLIL」 |
| [scripts/pipeline/yosys_common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh) | Yosys 段共用工具：`write_yosys_slang_script`（生成 .ys 脚本）与 `run_yosys_script`（安全执行 + OOM 识别） |
| [scripts/pipeline/common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 全流水线共用骨架：`require_file` / `require_executable` / `run_to_output` |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | `mkIlDerivation` 把本站包成派生，并按需注入 `YOSYS_SLANG_PER_FILE_EXTERNS=1` |
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 决定哪个模型开逐文件 extern：matmul 不开，tiny-stories-1m 开 |

## 4. 核心概念与源码讲解

### 4.1 yosys-slang 插件与 read_slang

#### 4.1.1 概念说明

Yosys 自带的 `read_verilog` 只支持 **Verilog-2005 的一个子集**，对 SystemVerilog 的现代语法（`logic` 类型、`always_comb` / `always_ff`、`enum`、`typedef`、`interface`、断言等）支持很弱。而 CIRCT 的 `--export-split-verilog` 产出的 `.sv` 大量使用了这些 SystemVerilog 特性。直接用 `read_verilog` 去读会满屏报错。

解决办法是 **`yosys-slang` 插件**：它把独立的、工业级 SystemVerilog 前端 **slang**（一个 C++ 实现的 SV 编译器，本就用于 lint 和综合前检查）包装成一个 Yosys 动态库插件（`.so`）。加载后，Yosys 就多了一条 `read_slang` 命令，能把完整 SystemVerilog 解析进 RTLIL。**slang 与 Yosys 都是开源的**——这正是 LLM2FPGA「全开源工具链」约束在 Yosys 段落地的关键。

Yosys 的插件机制很直白：在脚本里先 `plugin -i <slang.so>` 动态加载，之后 `read_slang` 命令才可用。

#### 4.1.2 核心流程

`sv_to_il.sh` 的整体流程是一条「**把 Yosys 命令写成脚本，再让 Yosys 执行脚本**」的两段式：

```
1. 校验 4 个入参：<yosys 可执行> <slang.so> <sources.f 清单> <输出 .il>
2. mktemp 一个临时 .ys 脚本，注册 trap 退出时清理
3. write_yosys_slang_script  往 .ys 写：
       plugin -i <slang.so>
       read_slang ...（按模式生成一条或多条）
4. 追写三条命令：hierarchy -check -top main / stat / write_rtlil <output>
5. run_yosys_script  执行：yosys -s <临时.ys>，并做 OOM 识别
```

为什么要「先写 .ys 脚本再执行」，而不是直接 `yosys -p "命令"` 内联？因为 `read_slang` 在逐文件 extern 模式下要生成**很多条**命令（每文件一条），写成脚本文件比拼成一条 `-p` 字符串更清晰、也更便于排错时直接看 `.ys` 内容。

#### 4.1.3 源码精读

入参与前置校验——四个位置参数，缺一报用法错；三个 `require_*` 是 [common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) 提供的「前置熔断」，缺文件/缺可执行权限立刻 `exit 2`，避免把错误拖到 Yosys 里才暴露：

读 [scripts/pipeline/sv_to_il.sh:12-21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh#L12-L21)（取四个参数、`require_executable`/`require_file` 校验、`mktemp` 临时 `.ys` 并 `trap rm` 清理）。关键几行：

```bash
yosys="${1:?usage: ...}"
yosys_slang_so="${2:?usage: ...}"
input="${3:?usage: ...}"     # sources.f 文件清单
output="${4:?usage: ...}"    # 输出 .il
...
tmp_ys="$(mktemp /tmp/ts_yosys_il_XXXXXX.ys)"
trap 'rm -f "$tmp_ys"' EXIT
```

调用共用函数生成脚本头部（`plugin -i` + `read_slang`），再追写三条收尾命令：

读 [scripts/pipeline/sv_to_il.sh:23-31](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh#L23-L31)（`write_yosys_slang_script` 生成插件加载与读 SV 命令，再 heredoc 追加 `hierarchy`/`stat`/`write_rtlil`，最后 `run_yosys_script` 执行）：

```bash
write_yosys_slang_script "$tmp_ys" "$yosys_slang_so" "$input"

cat >>"$tmp_ys" <<EOS
hierarchy -check -top main
stat
write_rtlil $output
EOS

run_yosys_script "sv_to_il" "$yosys" "$input" "SV->IL" -s "$tmp_ys"
```

注意 `plugin -i` 是在 `write_yosys_slang_script` 内部写进 `.ys` 的第一行，见 [scripts/pipeline/yosys_common.sh:14](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L14)：

```bash
echo "plugin -i ${yosys_slang_so}" >>"$script"
```

slang.so 的路径由 Nix 在调用时硬性指定为 `${yosysSlang}/share/yosys/plugins/slang.so`（见 [nix/pipeline.nix:91-94](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L91-L94)），保证每次用到的都是被 Nix pin 死的那个 slang 版本。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「不加载插件就没有 `read_slang` 命令」，建立对插件机制的直觉。

**操作步骤**（进入 `nix develop` 后，工具齐备）：

1. 找一个现成 `.sv` 文件，例如用 matmul 的产物（`nix build .#matmul-sv` 后在 `result/sv/` 里随便挑一个）。
2. 直接跑（**不**加载插件）：`yosys -p "read_slang 你的文件.sv"`。
3. 再跑（先加载插件）：`yosys -m $(nix eval --raw .#yosys-slang.out)/share/yosys/plugins/slang.so -p "read_slang --no-proc 你的文件.sv"`。

**需要观察的现象**：第 2 步应报 `ERROR: Can't find command read_slang` 之类；第 3 步能正常解析。

**预期结果**：确认 `read_slang` 完全依赖 `plugin -i`/`-m` 加载 slang.so 才存在。

> 如果暂时拿不到产物路径，可只做第 2 步验证「命令不存在」，第 3 步标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：能不能用 Yosys 自带的 `read_verilog` 代替 `read_slang`？为什么？
**参考答案**：不能。`read_verilog` 只支持 Verilog-2005 子集，而 CIRCT 导出的 SystemVerilog 用了 `logic` / `always_comb` 等 SV 特性，会大量报错。slang 才是完整的 SystemVerilog 前端。

**练习 2**：`plugin -i` 这行如果删掉，后面 `read_slang` 会怎样？
**参考答案**：Yosys 不认识 `read_slang` 这个命令，会直接报未知命令并中止。插件必须先加载，命令才注册进 Yosys。

---

### 4.2 文件分类与 extern 模式

#### 4.2.1 概念说明

`read_slang` 有两种读法，本站用一个环境变量 `YOSYS_SLANG_PER_FILE_EXTERNS` 切换：

| 模式 | 触发条件 | 命令形态 | 适用对象 |
| --- | --- | --- | --- |
| **单次 elaborate**（默认） | `YOSYS_SLANG_PER_FILE_EXTERNS` 未设或 0 | 一条 `read_slang --threads 1 --no-proc --top main 文件A 文件B ...`，slang 从 `main` 一次展开整个设计 | **matmul**（文件少、设计小） |
| **逐文件 extern** | `YOSYS_SLANG_PER_FILE_EXTERNS=1` | 每个文件一条 `read_slang --threads 1 --no-proc --extern-modules 文件X` | **tiny-stories-1m**（数千文件、4200 万 LUT） |

**为什么要分两种？** 单次 elaborate 简单稳健——slang 自己从顶层把所有实例化递归解析掉，缺模块就直接报错（参见项目早期在 `docs/project-management.org` 里记的 `unknown module instances required by top` 报错）。但它要求**把整个设计一次性载入内存**。对 TinyStories-1M 这种综合后约 4212 万 CLB LUT 的庞然大物，单次 elaborate 极易爆内存。逐文件 extern 把「一次大 elaborate」拆成「数千次小 parse」，**每次只解析一个文件**，峰值内存可控。

**`--extern-modules` 的作用**：逐文件解析时，当前文件往往实例化了「定义在别的文件里」的子模块。如果没有这条选项，slang 会因找不到定义而报错；加上它，找不到定义的模块就被当成 **blackbox 占位**保留下来，等读到真正定义它的那个文件时，Yosys 用真身替换占位。

**为什么要分类排序（早 / 中 / 晚）**：要让「模块的真实定义」尽量在「实例化它的使用者」**之前**被读入，从而尽量用真身、少用占位。于是：

- **早批**：`000_*.sv`（CIRCT 给文件加零填充数字前缀保证确定性，`000_` 是首个文件，常放接口/包/基础声明）、`*_generated_blackboxes.sv`（CIRCT 为 `hw.module.extern` 生成的黑盒 stub）、`*fp_primitives*.sv`（即上一站拷进来的 `zz_circt_fp_primitives.sv`，浮点原语实现）。这些是**叶子/基础定义**，先读。
- **中批**：其余所有生成模块，是设计主体。
- **晚批**：`main.sv`，顶层 `main`，**实例化所有子模块**，必须最后读——否则它引用的子模块此刻都还没进 Yosys，会全部退化成空黑盒。

谁开哪种模式由 [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) 决定：matmul 注册时**不带** `slangPerFileExternModules`（走默认单次），见 [nix/models.nix:8-18](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L18)；tiny-stories-1m 设了 `slangPerFileExternModules = true`，见 [nix/models.nix:20-33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L20-L33)（第 23 行）。

#### 4.2.2 核心流程

`write_yosys_slang_script` 的判定流程：

```
读取 sources.f，逐行：跳过空行、跳过 # 注释，其余收集进 slang_files
若 slang_files 为空  → 报错 exit 2
若 YOSYS_SLANG_PER_FILE_EXTERNS == 1：
    按文件名 basename 分三类：
      000_* / *_generated_blackboxes.sv / *fp_primitives*.sv  → early
      main.sv                                                   → late
      其余                                                       → middle
    按 early → middle → late 顺序，每文件输出一条：
      read_slang --threads 1 --no-proc --extern-modules <文件>
否则（单次模式）：
    输出一条：
      read_slang --threads 1 --no-proc --top main <所有文件>
```

数据流上，两种模式的对比：

```
单次模式 (matmul)：
  sources.f ─┐
             ├─▶ 一条 read_slang --top main  ─▶ slang 一次性 elaborate 全设计
  (全部文件) ┘

逐文件模式 (tiny-stories)：
  sources.f ─▶ 分类 ─▶ early (000_*, blackboxes, fp_primitives)
                       middle (其余模块)                ─▶ 逐文件 read_slang --extern-modules
                       late   (main.sv)                  （每文件一条，按早→中→晚顺序）
```

#### 4.2.3 源码精读

文件清单解析（跳空行、跳注释）与空清单保护：

读 [scripts/pipeline/yosys_common.sh:16-25](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L16-L25)（`while` 逐行读 `sources.f`，空行和 `#` 开头注释跳过，全空则 `exit 2`）：

```bash
while IFS= read -r line; do
  [[ -z "${line//[[:space:]]/}" ]] && continue   # 纯空白跳过
  [[ "${line#\#}" != "$line" ]] && continue       # # 注释跳过
  slang_files+=("$line")
done <"$input"
if [[ "${#slang_files[@]}" -eq 0 ]]; then
  echo "empty or comment-only file list: $input" >&2; exit 2
fi
```

分类的 `case` 与逐文件 `read_slang` 输出——本讲的核心：

读 [scripts/pipeline/yosys_common.sh:27-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L27-L46)（`YOSYS_SLANG_PER_FILE_EXTERNS=1` 分支：按 basename 用 `case` 分到 `early/middle/late` 三个数组，再按此顺序逐文件写出带 `--extern-modules` 的 `read_slang`）：

```bash
if [[ "${YOSYS_SLANG_PER_FILE_EXTERNS:-0}" == "1" ]]; then
  for line in "${slang_files[@]}"; do
    case "$(basename "$line")" in
      000_*.sv|*_generated_blackboxes.sv|*fp_primitives*.sv) early_files+=("$line") ;;
      main.sv)                                                 late_files+=("$line")  ;;
      *)                                                       middle_files+=("$line") ;;
    esac
  done
  for line in "${early_files[@]}" "${middle_files[@]}" "${late_files[@]}"; do
    echo "read_slang --threads 1 --no-proc --extern-modules $(printf '%q' "$line")" >>"$script"
  done
  return
fi
```

`printf '%q'` 把文件名做 shell 转义，防止路径里有空格/特殊字符破坏 `.ys` 脚本。

单次模式的兜底输出（一条命令带所有文件 + `--top main`）：

读 [scripts/pipeline/yosys_common.sh:49-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L49-L53)（默认分支：一条 `read_slang --top main` 把全部文件一起喂给 slang）：

```bash
{
  printf 'read_slang --threads 1 --no-proc --top main'
  printf ' %q' "${slang_files[@]}"
  printf '\n'
} >>"$script"
```

注意 `--top main` 只在单次模式出现——逐文件模式不指定 top，因为顶层 `main.sv` 只是被「最后读进来的一个普通文件」，层次由 `sv_to_il.sh` 后面的 `hierarchy -top main` 统一收口。

最后看 Nix 如何注入这个开关——`optionalString` 只在 `slangPerFileExternModules` 为真时 `export` 环境变量，且这个环境变量会被算进派生指纹（不同配置互不污染缓存）：

读 [nix/pipeline.nix:86-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L86-L95)（`mkIlDerivation`：按 `slangPerFileExternModules` 决定是否导出 `YOSYS_SLANG_PER_FILE_EXTERNS=1`，再调 `sv_to_il.sh`，把 `sources.f` 作为第 3 参数、`$out` 作为第 4 参数）。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：解释 `YOSYS_SLANG_PER_FILE_EXTERNS=1` 时为何要把 `000_*`、`main.sv`、其余文件分成早 / 中 / 晚三批读入。

**操作步骤**：

1. 打开 [scripts/pipeline/yosys_common.sh:27-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L27-L46)，对照上一站 [scripts/pipeline/hw_clean_to_sv.sh:76-82](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L76-L82)（`sources.f` 是 `find ... -name '*.sv' | sort` 的结果，外加末尾追写的 `zz_circt_fp_primitives.sv`）。
2. 假设某次 `hw_clean_to_sv.sh` 产出的 `sources.f` 含 4 个文件：`000_foo.sv`、`bar.sv`、`main.sv`、`zz_circt_fp_primitives.sv`。请**手写**出 `write_yosys_slang_script` 在逐文件 extern 模式下会生成的全部 `read_slang` 命令（含顺序）。
3. 再写出它在**单次模式**下会生成的唯一一条命令。

**需要观察的现象 / 预期结果（参考答案）**：

逐文件 extern 模式生成的 `.ys`（顺序：早 → 中 → 晚）：

```
read_slang --threads 1 --no-proc --extern-modules 000_foo.sv
read_slang --threads 1 --no-proc --extern-modules zz_circt_fp_primitives.sv   # *fp_primitives* 也属 early
read_slang --threads 1 --no-proc --extern-modules bar.sv                       # middle
read_slang --threads 1 --no-proc --extern-modules main.sv                      # late，最后
```

单次模式生成的 `.ys`（按 `sources.f` 里出现顺序，一条带 `--top main`）：

```
read_slang --threads 1 --no-proc --top main 000_foo.sv bar.sv main.sv zz_circt_fp_primitives.sv
```

**解释三批分类的原因**：

- **早批先读**：`000_*`（CIRCT 数字前缀的首文件，多为接口/包/基础声明）、`*_generated_blackboxes.sv`（extern 黑盒 stub）、`*fp_primitives*.sv`（浮点原语实现）都是**被别人实例化的叶子定义**。先读它们，后续文件实例化这些模块时就能直接用真身，而不是被 `--extern-modules` 退化成空黑盒。
- **晚批最后读**：`main.sv` 是顶层，**实例化了几乎所有子模块**。若先读它，它引用的所有模块此刻都不在 Yosys 里，会全部变成空黑盒，最终 RTLIL 的 `main` 之下将是一堆没有实现的占位，综合结果必然错误。
- **中批居中**：其余生成模块按文件名排序夹在中间，尽量保证「定义先于使用」。

> 这是「源码阅读型实践」，结论可直接从源码推得，无需运行；如要验证，可在 `nix build .#tiny-stories-1m-baseline-float-il -L` 的构建日志里找到实际 `.ys`（标注「待本地验证」实际文件名与数量）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 matmul **不**开 `YOSYS_SLANG_PER_FILE_EXTERNS`？
**参考答案**：matmul 文件少、设计小，单次 elaborate 又简单又稳健（slang 自己从 `--top main` 解析全部实例化，缺模块直接报错更易排错）。逐文件 extern 是为「设计大到单次 elaborate 会爆内存」的 TinyStories 准备的，小设计没必要拆。

**练习 2**：如果把 `main.sv` 错放进 `early_files`，会发生什么？
**参考答案**：`main` 最先读，它实例化的所有子模块此时都不在 Yosys 里，会被 `--extern-modules` 全部当成空 blackbox；后面读到真身时虽可能替换，但顺序错乱下极易留下空黑盒或引发层次冲突，最终 `hierarchy -check -top main` 大概率失败或得到一个几乎为空的设计。

---

### 4.3 stat 与 write_rtlil（含 OOM 处理）

#### 4.3.1 概念说明

读完 SV 后，本站还做三件落地的事：

1. **`hierarchy -check -top main`**：把 `main` 标记为顶层，递归检查整个设计层次是否完整（有没有实例化了却不存在的模块）。`-check` 让缺层次直接报错而非静默忽略。
2. **`stat`**：打印一份**文本**资源摘要到 stdout（多少 wire、多少 cell、按 cell 类型分类计数）。⚠️ 注意：这只是**构建期日志里的人读摘要**，**不是**最终 FPGA 资源报告。最终报告（LUT / FF / DSP / BRAM 与容量对比）在 [u5-l3 资源利用报告](u5-l3-utilization-report.md) 由 `write_utilization_report.py` 对**综合后**的 JSON 算出。`stat` 在这里数的 `$add`/`$mux` 等是 RTLIL 通用单元，还不是 FPGA LUT。
3. **`write_rtlil $output`**：把当前 Yosys 内部设计写成 RTLIL 文本到 `$out`，即本站产物 `<model>.il`，交给下游 `mkSynthJson`（`read_rtlil` 后跑 `synth_xilinx`）。

**关于 `--no-proc`（贯穿两种模式）**：slang 的 `read_slang` 默认会跑 Yosys 的 `proc` pass，把 `always` 块展开成 mux / 触发器。本站**故意加 `--no-proc` 跳过**，并且本站也**不跑** `flatten`/`opt`。这样产出的 `.il` 是「未展开的、层次化的」RTLIL，更紧凑、产出更快。理由有二：① **下游会自己跑**——`mkSynthJson` 里 `read_rtlil` 之后会执行 `synth_xilinx`，而 `synth_xilinx` 内部本就含 `proc`/`opt`/`flatten` 等步骤，这里先跑是重复劳动；② **对 4200 万 LUT 的设计，在本站就跑 `proc`/`flatten` 本身就是吃内存大户**，把它推迟到下游**可分阶段**的综合（见 [u5-l4 分阶段综合](u5-l4-staged-synth-memory-map.md)）里更可控。换言之，本站只做「读进来 + 校验层次 + 落盘」，把重活推后。

**OOM 识别（`run_yosys_script`）**：本站最常见、也最致命的失败不是语法错，而是**内存爆炸**——设计太大，Yosys 被内核 OOM killer 用 `SIGKILL`（信号 9）杀掉，进程退出码变成 \(137 = 128 + 9\)（某些情形下也直接是 `9`）。`run_yosys_script` 专门识别这两个码，打印「这通常是内存不足，请换更大内存的机器或削减模型复杂度」的友好提示；其它非零退出码（语法错、缺模块、层次校验失败等）当**普通失败**原样返回。两者的处置建议完全不同，所以必须区分。

#### 4.3.2 核心流程

`run_yosys_script` 的执行与诊断流程：

```
记录当前是否处于 set -e（errexit）状态
set +e                              ← 临时关掉 errexit，否则 yosys 一失败脚本就退、抓不到退出码
运行 yosys "$@"（这里即 yosys -s tmp.ys）
rc = $?                             ← 抓退出码
按原状态恢复 errexit（之前开就 set -e，之前关就 set +e）
若 rc == 137 或 rc == 9：
    向 stderr 打印 OOM 诊断（含 label / input / stage_hint / 建议）
return rc                           ← 把退出码原样返回，由调用方/pipefail 决定是否中止
```

关键技巧：因为整个脚本头是 `set -euo pipefail`（严格模式），Yosys 一旦非零退出就会**立刻中止**，根本没机会打印诊断。所以函数用「**临时关 errexit → 运行 → 抓码 → 恢复 errexit**」四步把退出码拿到手。`shift 4` 把前 4 个固定参数（`label`/`yosys`/`input`/`stage_hint`）吃掉，剩下的 `"$@"`（即 `-s tmp.ys`）原样透传给 Yosys。

#### 4.3.3 源码精读

`sv_to_il.sh` 追写的三条收尾命令（hierarchy / stat / write_rtlil）见上文 [4.1.3](#413-源码精读) 引用的 [scripts/pipeline/sv_to_il.sh:25-29](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh#L25-L29)。

`run_yosys_script` 的完整实现——本讲第二个核心：

读 [scripts/pipeline/yosys_common.sh:56-84](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/yosys_common.sh#L56-L84)（`run_yosys_script`：记 errexit 状态 → `set +e` 跑 yosys → 恢复 errexit → 识别 137/9 打 OOM 提示 → `return "$rc"`）：

```bash
run_yosys_script() {
  local label="$1"; local yosys="$2"; local input="$3"; local stage_hint="$4"
  local errexit_was_on=0
  shift 4

  if [[ $- == *e* ]]; then errexit_was_on=1; fi   # 当前是否 set -e

  set +e                                            # 临时关，抓退出码
  "$yosys" "$@"
  local rc=$?
  if [[ "$errexit_was_on" -eq 1 ]]; then set -e; else set +e; fi

  if [[ "$rc" -eq 137 || "$rc" -eq 9 ]]; then
    echo "[$label] ERROR: Yosys was killed while processing '$input' (exit code $rc)." >&2
    echo "[$label] This is usually an out-of-memory condition." >&2
    echo "[$label] Try a host with more RAM, or reduce model complexity before ${stage_hint}." >&2
  fi
  return "$rc"
}
```

`$-` 是 bash 的当前选项串（含 `e` 表示 errexit 开着）；`shift 4` 后 `"$@"` 透传 `-s "$tmp_ys"`；`stage_hint`（本站传 `"SV->IL"`）只用在报错文案里，提示用户「在哪个阶段前削减复杂度」。注意函数**只诊断、不吞错**——它 `return "$rc"`，非零码最终仍会让 `set -e`/`pipefail` 触发脚本中止，CI 照样红。

#### 4.3.4 代码实践

**实践目标**：验证 `run_yosys_script` 能区分「普通失败」与「OOM」，并理解 `stat` 不是最终资源报告。

**操作步骤**：

1. 在 `nix develop` 里手写一个会**普通失败**的最小 `.ys`：让它读一个不存在/有语法错的 SV，例如 `echo 'read_slang --no-proc /tmp/no_such.sv' > /tmp/bad.ys; yosys -m .../slang.so -s /tmp/bad.ys; echo "rc=$?"`。观察退出码（通常非 0，但**不是** 137/9），且没有 OOM 提示。
2. （可选，标注「待本地验证」）人为制造 OOM：`systemd-run --scope -p MemoryMax=64M yosys -s tmp.ys` 跑一个稍大的 `.il` 构建，观察退出码 137 及 `run_yosys_script` 风格的 OOM 文案。
3. 跑 `nix build .#matmul-il -L`，在构建日志里找到 `1. Printing statistics.` 那段（即 `stat` 输出）。

**需要观察的现象**：

- 普通失败：退出码非 0（非 137/9），stderr 是 slang/Yosys 自己的报错，**无** `[sv_to_il] ERROR: ... out-of-memory` 字样。
- OOM：退出码 137，stderr 出现 `This is usually an out-of-memory condition.` 提示。
- `stat` 输出里数的是 `$add`/`$mul`/`$mux` 之类 RTLIL 通用单元，**不是** `clb_luts`/`FF`。

**预期结果**：确认「137/9 → OOM 提示，其它非零 → 普通失败」的分流；并确认本站 `stat` 只是过程日志，最终 FPGA 资源数字要到下游综合后的 JSON 才有。

#### 4.3.5 小练习与答案

**练习 1**：本站 `stat` 打印的 cell 数能直接当成「这个设计要多少 LUT」吗？
**参考答案**：不能。`stat` 数的是 RTLIL 通用单元（`$add`、`$mux`、`$dff` 等），与 FPGA 的 LUT/FF/DSP 不是一回事。要得到 FPGA 资源，必须先 `synth_xilinx` 把这些通用单元映射（techmap）到 Xilinx 原语，再统计——那是 [u5-l3](u5-l3-utilization-report.md) 的事。

**练习 2**：为什么 OOM 要用退出码 `137` 判断，而不是 `1`？
**参考答案**：`1` 是程序自己 `exit(1)` 报错的常见码（语法错、层次错都属于这类，「换个写法/修源码」可能解决）；`137 = 128 + 9` 是进程被 `SIGKILL` 杀掉的标志码，几乎都是内核 OOM killer 所为（「换大内存机器/削减模型」才能解决）。两类失败的处置方向完全不同，所以分别识别、给不同建议。

**练习 3**：`run_yosys_script` 里的 `set +e ... set -e` 四步如果省掉，会发生什么？
**参考答案**：脚本头是 `set -e`，Yosys 一非零退出就会**立刻中止整个脚本**，根本到不了判断 `rc`/打印 OOM 提示那一步；于是 OOM 时用户只会看到一个干巴巴的非零退出码，拿不到「内存不足、换机器」的关键诊断。

## 5. 综合实践

把本讲三个模块串起来，做一次「**从 sources.f 到 .ys 到 OOM 诊断**」的完整推演。

**任务**：假设你是新成员，被要求回答「为什么 TinyStories-1M 的 `il` 派生构建会在这一站失败、日志里会出现什么、该怎么处置」。

**步骤**：

1. **追接口**：从 [nix/pipeline.nix:86-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L86-L95) 的 `mkIlDerivation` 出发，确认它把 `${sv}/sources.f` 传给 `sv_to_il.sh` 作第 3 参数，并因 `slangPerFileExternModules=true`（[nix/models.nix:23](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L23)）导出了 `YOSYS_SLANG_PER_FILE_EXTERNS=1`。
2. **推脚本**：据此说明 `write_yosys_slang_script` 会走**逐文件 extern** 分支，把数千个 `.sv` 按早/中/晚三批各生成一条 `read_slang --extern-modules`，最后才是 `main.sv`，再追写 `hierarchy`/`stat`/`write_rtlil`。
3. **判失败**：若构建在某一文件 `read_slang` 时被杀、退出码 137，写出 `run_yosys_script` 会向 stderr 打印的三行诊断（含 `label="sv_to_il"`、`stage_hint="SV->IL"`）。
4. **给建议**：结合 [u1-l4](u1-l1-project-overview.md) 的「超配约 141 倍」结论，说明为什么「换更大内存的机器」只能治标——根因是设计本身就比目标芯片大约 141 倍，真正的出路是 [u7-l3](u7-l3-roadmap-and-resource-optimization.md) 讲的资源最小化（量化、换掉 Handshake 方言、用板载内存等）。

**预期产出**：一段能自圆其说的分析，覆盖「接口 → 脚本生成 → 失败诊断 → 处置方向」全链路。

> 本任务以源码阅读 + 推演为主；若要在本机实跑，用 `nix build .#tiny-stories-1m-baseline-float-il -L`，但因设计极大，普通机器很可能真的 OOM（标注「待本地验证」）。

## 6. 本讲小结

- 本站 `sv_to_il.sh` 用 **`plugin -i slang.so`** 加载 yosys-slang 插件，以 **`read_slang`** 把 CIRCT 导出的 SystemVerilog 读进 Yosys，产出 **RTLIL（`.il`）** 交给下游综合。
- 用 slang 而非自带 `read_verilog`，是因为只有 slang 能完整解析 SystemVerilog（`logic`/`always_comb` 等），且 slang + Yosys 全开源，满足项目「全开源工具链」约束。
- `read_slang` 有两种模式：matmul 走**单次 elaborate**（一条命令 + `--top main`，简单稳健）；tiny-stories-1m 走**逐文件 extern**（每文件一条 + `--extern-modules`，峰值内存可控）。
- 逐文件 extern 模式把文件按 `000_*`/黑盒/浮点原语（早）、其余模块（中）、`main.sv`（晚）三批读入，保证「定义先于使用」、顶层最后读。
- 收尾的 `hierarchy -check -top main` 校验层次、`stat` 打印过程性摘要（**非**最终资源报告）、`write_rtlil` 落盘；`--no-proc` 把 `proc`/`flatten` 等重活推迟到下游可控的分阶段综合。
- `run_yosys_script` 用「临时关 errexit → 抓退出码 → 恢复」识别 `137`/`9`，区分 **OOM**（换大内存/削减模型）与**普通失败**（修源码），给用户不同建议。

## 7. 下一步学习建议

- 下一讲 [u5-l2 matmul 端到端综合与比特流生成](u5-l2-matmul-synth-and-bitstream.md) 会消费本站产出的 `.il`：用 `read_rtlil` + `synth_xilinx` 出 JSON，再经 nextpnr → FASM → 比特流，并把 matmul 的自测外壳接上去。建议先看清 `mkSynthJson` 如何把本站的 `.il` 与一个顶层 `top.sv` 拼到同一条 Yosys 命令里。
- 若你对「为什么本站不跑 proc」还想深挖，可读 [u5-l4 分阶段 Yosys 综合与 targeted memory_map](u5-l4-staged-synth-memory-map.md)，看 `synth_xilinx` 如何被拆成 `-run` 区间逐步执行、`proc`/`flatten` 在哪一段发生。
- 想理解本站逐文件 extern 模式服务的「超配约 141 倍」结论从何而来，可回看 [u1-l4 跑通第一个构建命令](u1-l4-first-build-and-reproduce.md) 与 [u5-l3 资源利用报告](u5-l3-utilization-report.md)。
