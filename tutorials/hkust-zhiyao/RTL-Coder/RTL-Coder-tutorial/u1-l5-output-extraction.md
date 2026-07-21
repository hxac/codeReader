# 输出后处理与代码抽取（u1-l5）

> 阶段：入门层（beginner） · 依赖：u1-l3《快速上手：加载预训练模型做推理》
> 本讲只解决一件事：**模型吐出来的一长串文本，怎么变成一段能直接送进仿真器的干净 Verilog 模块**。你会看到 RTL-Coder 用几个字符串切分小技巧（`endmodule`、`endmodulemodule`、`testbench`）完成了这件事，并且 Mistral 版和 Deepseek 版的处理方式不同。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 解释**为什么不能直接拿 `model.generate` 的输出当 Verilog 代码用**——模型可能不停、可能附带 testbench、可能连写第二个模块；
- 掌握通用截断技巧 `s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"`，并能讲清它「**取最后一个 `endmodule` 之前的内容、再补一个干净的 `endmodule`**」的含义；
- 看懂用 `tb_module` / `testbench` 关键字定位并剔除测试台段落的逻辑；
- 说出 RTLCoder-Deepseek 版本的特殊关键字 `endmodulemodule` 的由来，以及它为何要改用「从左切第一个」的 `split`；
- 写出一个函数 `extract_verilog(raw, model_type)`，对一段含 testbench 的假输出做清洗测试。

本讲不需要你懂深度学习，只需要会读 Python 字符串操作（`split` / `rsplit` / `find` / `rfind`）。

---

## 2. 前置知识

### 2.1 Verilog 模块的基本结构

一段最小的 Verilog 模块长这样：

```verilog
module half_adder(
    input  a,
    input  b,
    output sum,
    output carry
);
    assign sum   = a ^ b;
    assign carry = a & b;
endmodule
```

关键事实：**每个模块都以 `module` 开头、以 `endmodule` 结尾**。`endmodule` 是一个明确的「模块结束」边界符。这正是 RTL-Coder 后处理能够成立的基础——只要找到 `endmodule`，就找到了一个模块的尾巴。

### 2.2 `split` 与 `rsplit`：从左切还是从右切

这两个 Python 字符串方法是本讲的主角，先复习一下：

- `'a_b_c'.split('_', 1)` → `['a', 'b_c']` —— **从左**找第一个分隔符，最多切 1 刀。`[0]` 是**第一个分隔符之前**的内容。
- `'a_b_c'.rsplit('_', 1)` → `['a_b', 'c']` —— **从右**找最后一个分隔符，最多切 1 刀。`[0]` 是**最后一个分隔符之前**的内容。

> 🔑 一句话记忆：`split` 拿「头」（第一个分隔符前），`rsplit` 拿「除尾以外」（最后一个分隔符前）。当字符串里**只有一个**分隔符时，两者结果相同；只有出现多个分隔符时它们才有区别——而 RTL-Coder 正是利用了这个区别来应对两种模型的不同输出习惯。

### 2.3 模型输出 `s_full` 是什么

回顾 u1-l3，调用 `model.generate(...)` 后会得到一串 token id，用 `tokenizer.decode(...)` 解码成文本，记作 `s_full`。它通常包含：

- 你期望的**设计模块代码**（含 `endmodule`）；
- 有时后面还跟着一段**测试台（testbench）**——因为训练数据里有些样本带 testbench，模型会「顺手」续写出来；
- 对于 Deepseek 版，模型**不会自动停止**，会在写完一个模块后**紧接着开始写第二个模块**，于是出现连写关键字 `endmodulemodule`（上一个 `endmodule` 紧贴下一个 `module`）。

`extract_verilog` 要做的，就是把 `s_full` 里这些「多余的东西」砍掉，只留下一个干净、完整、闭合的模块。

> 一个容易忽略的细节：`s_full` 在不同脚本里**范围不一样**。README 的最小示例 `s_full = tokenizer.decode(sample[0])` 解码的是**「prompt + 生成内容」**整段；而两个基准脚本里 `s_full = tokenizer.decode(output[len(inputs[0]):]...)` 用切片跳过了 prompt，只解码**生成内容**。这对后处理没影响——因为我们要找的 `endmodule` 只出现在生成内容里——但你脑子里要清楚 `s_full` 到底含不含 prompt。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `benchmark_inference/test_on_verilog-eval.py` | VerilogEval 基准推理脚本 | 第 86 行得到 `s_full`；第 103 行的 Mistral 主截断；第 106–111 行的 testbench 剔除；第 87–100 行注释掉的 Deepseek 版本 |
| `benchmark_inference/test_on_rtllm.py` | RTLLM 基准推理脚本 | 第 79 行得到 `s_full`；第 95 行主截断；第 97–102 行 testbench 剔除；第 80–93 行注释掉的 Deepseek 版本 |
| `README.md` | 项目说明，含最小推理示例 | 第 53 行对 `endmodulemodule` 的文字说明；第 161–176 行**完整且未注释**的 Deepseek 后处理代码块（最权威的参考） |

> 注意：两个基准脚本里**默认启用的是 Mistral 版**逻辑（未注释），Deepseek 版逻辑被整段注释掉了，需要你手动取消注释才能用于 Deepseek 模型。README 第 33 行也专门强调了这一点。

---

## 4. 核心概念与源码讲解

### 4.1 核心截断：用 `rsplit('endmodule')` 切出干净模块并补全 `endmodule`

#### 4.1.1 概念说明

最朴素的问题：模型输出里可能带着尾随的空行、解释性文字、甚至半个多余模块，怎么保证最后落盘的是一段「恰好以一个 `endmodule` 收尾」的代码？

RTL-Coder 给 Mistral 版用的通用解法只有一行：

```python
s = s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"
```

它的直觉是：**「`endmodule` 是模块的尾巴，那我就以最后一个 `endmodule` 为界，把它之前的内容留下，再补一个干净的 `endmodule` 当结尾。」**

为什么用 `rsplit`（从右切）而不是 `split`（从左切）？因为 Mistral 版在写完模块后通常会**自动停止**，整段输出里往往只有「真正想要的那个模块」加少量尾随噪声。用「最后一个 `endmodule`」当边界，能保留主体内容、只砍掉 `endmodule` 之后的垃圾。

#### 4.1.2 核心流程

截断的伪代码：

```
输入 s_full（模型解码出的文本）
1. 用 rsplit('endmodule', 1) 从右至多切一刀
2. 取 [0]：即「最后一个 endmodule 之前」的全部内容（不含 endmodule）
3. 在末尾补上 "\n" + "endmodule"
输出 s：一段以唯一一个干净 endmodule 结尾的代码
```

用一个对照表看清 `split` 与 `rsplit` 在多 `endmodule` 输出下的差别（设 `s_full = "mod_A... endmodule\nmod_B... endmodule\n垃圾"`）：

| 写法 | 切割位置 | `[0]` 得到 | 追加 `endmodule` 后 |
| --- | --- | --- | --- |
| `split('endmodule',1)[0]` | 第一个 `endmodule` | `"mod_A... "` | 只保留模块 A |
| `rsplit('endmodule',1)[0]` | 最后一个 `endmodule` | `"mod_A... endmodule\nmod_B... "` | 保留模块 A 和 B |

可以看到 RTL-Coder 选 `rsplit` 是有意的：它假设「主体的多模块都该留，只砍最后那个 `endmodule` 之后的尾巴」。当模型只输出一个模块时（最常见情况），两者结果一致——这也是练习里「第一个 `endmodule` 之前」这种朴素说法成立的条件。

#### 4.1.3 源码精读

先看 `s_full` 怎么来的——基准脚本解码时**跳过了 prompt**，只取生成部分：

[benchmark_inference/test_on_verilog-eval.py:L86](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L86) —— 用 `output[len(inputs[0]):]` 切掉 prompt 长度，只解码模型新生成的内容，并 `skip_special_tokens=True` 去掉特殊符。

RTLLM 脚本用的是同一手法：

[benchmark_inference/test_on_rtllm.py:L79](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L79) —— 同样切片跳过 prompt，得到纯生成文本 `s_full`。

然后是核心截断这一行（两个脚本里完全一样）：

[benchmark_inference/test_on_verilog-eval.py:L103](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L103) —— Mistral 版的主截断：`rsplit('endmodule',1)[0]` 取最后一个 `endmodule` 之前的内容，再补 `\nendmodule`。

[benchmark_inference/test_on_rtllm.py:L95](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L95) —— RTLLM 脚本里同一行，证明这是项目通用的 Mistral 处理范式。

README 的注释里也明确说：Mistral 版「会自动停止」，但你**仍然可以用 `endmodule` 关键字来抽取代码部分**：

[README.md:L178-L186](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L178-L186) —— 对 RTLCoder-v1.1（Mistral）只需 `decode(sample[0])` 即可，但仍建议用 `endmodule` 抽取。

> 🔑 这一行是整个后处理的「地基」。后面 4.2（剔 testbench）和 4.3（Deepseek 的 `endmodulemodule`）都是在这个地基上做的「二次修正」。

#### 4.1.4 代码实践

**目标**：实现并验证「核心截断」这一步，亲眼看到尾随垃圾被砍掉、结尾被补成干净的 `endmodule`。

**步骤**：在仓库根目录新建一个临时脚本（运行完即可删除，不要提交）：

```python
# 示例代码：核心截断实验
def truncate_endmodule(s_full: str) -> str:
    return s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"

raw = "module half_adder(input a, input b, output sum, output carry);\nassign sum=a^b;\nendmodule\n\n// 下面是模型乱写的尾巴\nmodule garbage();\n"
print("=== 清洗后 ===")
print(truncate_endmodule(raw))
print("=== 结尾是否为 endmodule ===", truncate_endmodule(raw).rstrip().endswith("endmodule"))
```

**需要观察的现象**：`rsplit` 从右切，于是输出里**保留了 `module garbage()` 那一段**（因为它在最后一个 `endmodule` 之前——注意 `raw` 里只有一个 `endmodule`，所以「之前」几乎包含全部内容），只在最后补了一个 `endmodule`。结尾断言为 `True`。

**预期结果**：清洗后字符串以 `endmodule` 收尾；`garbage` 段因为出现在唯一一个 `endmodule` 之前，会被保留——这正说明单靠 4.1 这一行**不足以**去掉多余的第二个模块，需要 4.3 的 `endmodulemodule` 逻辑或上层把第二个模块当作 testbench 处理。

> 待本地验证：如果你的 `raw` 里 `endmodule` 出现两次（真正的多模块），观察 `rsplit` 会保留两个模块、而 `split` 只留第一个，亲手验证 4.1.2 的对照表。

#### 4.1.5 小练习与答案

**练习 1**：把 `rsplit('endmodule', 1)` 里的 `1` 去掉，变成 `rsplit('endmodule')`，会对结果产生什么影响？
**参考答案**：`1` 是「最多切几刀」的 `maxsplit`。去掉它会对**每一个** `endmodule` 都切一刀，`[0]` 就变成了「第一个 `endmodule` 之前」的内容（等价于从左切的头部）。于是多模块输出里只会保留第一个模块之前的部分，可能把主体内容也切掉。所以这个 `1` 必不可少。

**练习 2**：为什么代码写成 `+ "\n" + "endmodule"` 而不是 `+ "endmodule"`？
**参考答案**：`rsplit(...)[0]` 取到的内容**不含** `endmodule` 本身（分隔符会被切掉），必须手动补回来；中间加 `"\n"` 是为了和模块体之间有个换行，让最终代码排版规整、便于落盘和阅读。

---

### 4.2 剔除 testbench：用 `tb_module` / `testbench` 关键字定位

#### 4.2.1 概念说明

只做 4.1 的截断还不够。模型有时在写完设计模块后，会**接着续写一段测试台（testbench）**——里面有 `initial` 块、`$display`、对设计模块的例化等。这段 testbench **不是设计代码**，如果一起落盘，会让仿真器/综合工具把「设计 + 测试台」当成一个文件去编译，往往报错。

RTL-Coder 的处理方式很「暴力但有效」：**用关键字定位 testbench 的起点，把它及之后的内容全部砍掉**，再补一个干净的 `endmodule` 闭合设计模块。

它用的关键字有两个，按优先级：

1. `tb_module`：模型给 testbench 顶层模块常用的命名；
2. `testbench`：更通用的字样，作为兜底。

#### 4.2.2 核心流程

伪代码：

```
在已经做过 4.1 截断的字符串 s 上：
1. index = s.rfind('tb_module')      # 从右找 tb_module
2. 如果找不到（index == -1）：
       index = s.find('testbench')   #   再从左找 testbench 兜底
3. 如果还是找到了（index != -1）：
       s_tmp = s[:index]             #   砍掉 testbench 起点 index 及之后
       s = s_tmp.rsplit('endmodule', 1)[0] + "\n" + "endmodule"   # 重新闭合
```

注意第 3 步又用了一次 4.1 的「`rsplit('endmodule',1)[0]` + 补 `endmodule`」技巧——因为砍掉 testbench 后，`s_tmp` 的末尾可能正好停在某个 `endmodule` 之后（设计模块的尾巴被一起切走了一段），需要重新补一个 `endmodule` 来闭合设计模块。

#### 4.2.3 源码精读

verilog-eval 脚本里这段逻辑紧跟在主截断之后：

[benchmark_inference/test_on_verilog-eval.py:L105-L111](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L105-L111) —— 先 `rfind('tb_module')`，找不到再 `find('testbench')`；命中则砍掉该位置之后内容并重新闭合 `endmodule`。第 105 行的注释 `# the model may output testbench after the design code` 点明了意图。

RTLLM 脚本里是一模一样的处理：

[benchmark_inference/test_on_rtllm.py:L97-L102](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L97-L102) —— 同样的 `tb_module` → `testbench` 两级关键字兜底，命中后 `rsplit` 重闭合。

README 的最小示例也包含同样逻辑（在 Deepseek 分支里）：

[README.md:L170-L175](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L170-L175) —— `rfind('tb_module')` 优先、`find('testbench')` 兜底，命中即截断重闭合，与基准脚本完全一致。

> 🔑 这是一个典型的「关键字兜底」模式：先用更具体的 `tb_module`（rfind 从右找，假设 testbench 在设计模块之后），找不到再用更宽泛的 `testbench`（find 从左找）。两级关键字提高了命中率，又尽量减少误伤。

#### 4.2.4 代码实践

**目标**：在 4.1 的函数上叠加 testbench 剔除，验证一段「设计模块 + testbench」的假输出被正确瘦身。

**步骤**：

```python
# 示例代码：叠加 testbench 剔除
def remove_testbench(s: str) -> str:
    index = s.rfind('tb_module')
    if index == -1:
        index = s.find('testbench')
    if index != -1:
        s_tmp = s[:index]
        s = s_tmp.rsplit('endmodule', 1)[0] + "\n" + "endmodule"
    return s

s_with_tb = (
    "module half_adder(input a, input b, output sum, output carry);\n"
    "assign sum=a^b;\nendmodule\n"
    "`timescale 1ns/1ps\n"
    "module tb_module();\nreg a,b; wire s,c;\ninitial begin $display(); end\nendmodule\n"
)
cleaned = remove_testbench(s_with_tb)
print(cleaned)
print("=== 是否还含 testbench ===", ('tb_module' in cleaned) or ('testbench' in cleaned))
```

**需要观察的现象**：处理后的字符串里不再出现 `tb_module`/`testbench`，且以 `endmodule` 收尾；`tb_module` 起点之后那整段（含 `initial`、`$display`）被砍掉。

**预期结果**：末尾断言为 `False`（已不含 testbench 关键字）；`half_adder` 模块完整保留并被重新闭合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tb_module` 用 `rfind`（从右找），而 `testbench` 用 `find`（从左找）？
**参考答案**：`tb_module` 是 testbench 顶层模块名，通常只出现一次且在设计模块**之后**，从右找能精确定位到「最后那段 testbench」的起点。`testbench` 是更宽泛的字样（可能出现在注释、文件头里），用从左找会容易误伤前面的内容——所以它只是兜底，且优先级低于 `tb_module`。两者方向不同是为了匹配各自的「最可能出现位置」。

**练习 2**：如果模型输出的 testbench 模块名既不叫 `tb_module`、也不含 `testbench` 字样（比如直接叫 `sim_top`），这套逻辑会怎样？
**参考答案**：两个关键字都找不到（`index == -1`），于是直接跳过剔除步骤，testbench 会被保留下来落盘——这正是该方案的局限。要解决就得扩充关键字清单（加入 `sim_top` 等）或换用更强的结构化解析。这也提醒我们：这套后处理是**基于经验关键字的启发式方法**，并非严格语法分析。

---

### 4.3 Deepseek 特殊处理：`endmodulemodule` 关键字与 `top_module`

#### 4.3.1 概念说明

RTLCoder-Deepseek-v1.1 是精度最高的版本，但它有一个「怪癖」：**即使该输出的代码已经写完，模型也不会自动停止**，会继续往下生成。它的典型续写方式是——写完一个 `endmodule` 后**紧接着**开始写下一个 `module`，于是输出里会出现连写关键字 **`endmodulemodule`**（`endmodule` 的 `e` 与下一个 `module` 的 `m` 之间没有空格/换行）。

> README 在介绍模型时明确点出了这一点：

[README.md:L53](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L53) —— 说明 Deepseek 版不会自动停止，需要根据关键字 `endmodulemodule` 抽取代码、并在结尾补 `endmodule`。

于是 RTL-Coder 把 `endmodulemodule` 当成**「第一个模块结束、第二个模块开始」的边界信号**：只要在输出里检测到 `endmodulemodule`，就从**它之前**切断，只保留第一个模块——这正是改用 `split`（从左切第一个）的原因，与 4.1 里 Mistral 版用 `rsplit`（保留到最后一个）恰恰相反。

此外还有一道针对 verilog-eval 的 `top_module` 兜底：verilog-eval 的评测会把待测模块重命名/包装成 `top_module`，模型有时会自发多写一个 `top_module` 包装层，需要一并砍掉。

#### 4.3.2 核心流程

Deepseek 版的完整后处理（对应 README 那段未注释代码）伪代码如下：

```
输入 s_full
1. 主截断（注意这里和 Mistral 不同）：
     if s_full 里有 'endmodulemodule'（split 切出来正好两段）：
         s = s_full.split('endmodulemodule', 1)[0] + "\n" + "endmodule"   # 从左切，只留第一个模块
     else：
         s = s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"        # 没有 endmodulemodule 就退回 rsplit
2. top_module 兜底：
     if 'top_module' in s：
         s = s.split('top_module', 1)[0]                                  # 砍掉 top_module 包装层起点
         s = s.rsplit('endmodule', 1)[0] + "\n" + "endmodule"            # 重新闭合设计模块
3. testbench 剔除：同 4.2（tb_module / testbench）
```

> 🔑 注意第 1 步里判断「有没有 `endmodulemodule`」的写法是 `if len(s_full.split('endmodulemodule', 1)) == 2:`——即「切出来正好两段」等价于「至少出现一次」。这是一种常见但不直观的「包含判断」写法，等价于 `'endmodulemodule' in s_full`。

#### 4.3.3 源码精读

最权威的参考是 README 里**完整且未注释**的 Deepseek 后处理代码块（基准脚本里同一段是被注释掉的）：

[README.md:L161-L176](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L161-L176) —— Deepseek 版完整后处理：先按 `endmodulemodule` 切（第 163–166 行），再处理 `top_module`（第 167–169 行），最后剔 testbench（第 170–175 行）。

逐段看：

- 第 161–162 行：注释说明 Deepseek 不会自动停止，需要按 `endmodulemodule` 抽取；
- 第 163–164 行：检测到 `endmodulemodule` 时，用 `split`（从左）取第一个模块、补 `endmodule`；
- 第 165–166 行：否则退回 `rsplit('endmodule',1)` 的通用做法；
- 第 167–169 行：若出现 `top_module`，砍掉它及之后内容，重新闭合；
- 第 170–175 行：与 4.2 完全一致的 testbench 剔除。

基准脚本里把这段 Deepseek 逻辑整段**注释保留**，方便你切换模型时启用：

[benchmark_inference/test_on_verilog-eval.py:L87-L100](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L87-L100) —— 注释掉的 Deepseek 抽取分支，结构与 README 完全一致；第 101 行注释提示「若用 Mistral 版，只需用下面那行」。

[benchmark_inference/test_on_rtllm.py:L80-L93](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L80-L93) —— RTLLM 脚本里同样注释保留的 Deepseek 分支。

> 🔑 Mistral 与 Deepseek 后处理的**唯一本质差异**就在主截断这一步：Mistral 用 `rsplit`（信「模型会停、保留到最后一个 `endmodule`」），Deepseek 用「`endmodulemodule` 触发的 `split`」（信「模型不停、第一个模块才是想要的」）。testbench 剔除两者完全相同。这就是 README 第 33 行强调「测 Deepseek 要看 `test_on_verilog-eval.py` 里的注释」的根本原因。

#### 4.3.4 代码实践

**目标**：把 `extract_verilog(raw, model_type)` 扩展出 `'deepseek'` 分支，体会 `split` 与 `rsplit` 在同一份输入下的不同产出。

**步骤**：

```python
# 示例代码：Deepseek 分支实验
def extract_core_deepseek(s_full: str) -> str:
    if len(s_full.split('endmodulemodule', 1)) == 2:        # 出现连写关键字
        s = s_full.split('endmodulemodule', 1)[0] + "\n" + "endmodule"   # 从左切，只留第一个模块
    else:
        s = s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"
    if s.find('top_module') != -1:
        s = s.split('top_module', 1)[0]
        s = s.rsplit('endmodule', 1)[0] + "\n" + "endmodule"
    return s

raw = (
    "module half_adder(input a, input b, output sum, output carry);\n"
    "assign sum=a^b;\nendmodule"          # 注意：Deepseek 紧接着写第二个模块，于是连写
    "module second_one(input x); assign x=0; endmodule\n"
)
print("=== Deepseek 抽取（含 endmodulemodule）===")
print(repr(extract_core_deepseek(raw)))
print("结尾:", extract_core_deepseek(raw).rstrip()[-len("endmodule"):])
```

**需要观察的现象**：因为 `raw` 里出现了 `endmodulemodule`（`endmodule` 直接接 `module second_one`），`split('endmodulemodule', 1)[0]` 会取到第一个模块体（不含尾巴），再补 `endmodule`，于是**第二个模块被干净地丢弃**，输出只剩 `half_adder`。

**预期结果**：输出里只剩 `half_adder` 一个模块、以 `endmodule` 收尾；不含 `second_one`。作为对比，把同样的 `raw` 喂给 4.1 的 Mistral 版 `rsplit`，你会发现它**会保留两个模块**——亲手验证「`split` 留第一个、`rsplit` 留到最后一个」的差异。

> 待本地验证：若你构造的 `raw` 里没有 `endmodulemodule`（比如两个模块之间有换行），则 Deepseek 分支会走 `else` 退回 `rsplit`，此时与 Mistral 版行为一致。这说明 `endmodulemodule` 是触发「只留第一个模块」的唯一开关。

#### 4.3.5 小练习与答案

**练习 1**：`if len(s_full.split('endmodulemodule', 1)) == 2:` 这个判断等价于哪个更直观的写法？为什么项目偏偏用 `split` 来判断？
**参考答案**：等价于 `if 'endmodulemodule' in s_full:`。项目用 `split` 一石二鸟：既完成了「是否包含」的判断（结果长度为 2 即包含），又在下一行直接复用了切分结果 `s_full.split('endmodulemodule', 1)[0]`，省去再切一次。这是一种紧凑但略牺牲可读性的写法。

**练习 2**：为什么 Deepseek 版用 `split`（从左切第一个），而 Mistral 版用 `rsplit`（从右切最后一个）？
**参考答案**：因为两个模型的「输出习惯」相反。Deepseek **不会自动停止**，写完第一个模块会接着写第二个、第三个……第一个模块才是评测想要的，所以要从左切、只留第一个（用 `endmodulemodule` 精确定位第一/第二模块的边界）。Mistral **会自动停止**，输出里通常只有一个真正想要的模块加少量尾随噪声，所以从右切、保留到最后一个 `endmodule`、只砍后面的垃圾。**切分方向编码了对模型行为的假设**——这是本讲最值得记住的设计思想。

---

## 5. 综合实践

把 4.1–4.3 串起来，完成本讲的核心交付物：一个统一的 `extract_verilog(raw, model_type)`，能根据模型类型清洗输出，并对含 testbench、含连写模块的假输出做测试。

**任务要求**：

1. 实现 `extract_verilog(raw, model_type)`，`model_type` 取 `'mistral'` 或 `'deepseek'`；
2. 两条分支共享「testbench 剔除」逻辑，只在「主截断」处不同：
   - `'mistral'`：直接 `rsplit('endmodule',1)[0] + "\nendmodule"`；
   - `'deepseek'`：先按 `endmodulemodule`/`top_module` 处理，再退回 `rsplit`；
3. 主截断后，统一走一遍 `tb_module`/`testbench` 剔除；
4. 用至少三组假输出验证：① 干净单模块；② 模块 + testbench；③ 模块连写第二个模块（Deepseek 风格）。

**参考实现**（示例代码，请补全测试并运行）：

```python
# 示例代码：统一的 Verilog 输出抽取
def _close_endmodule(text: str) -> str:
    """取最后一个 endmodule 之前的内容，补一个干净的 endmodule。"""
    return text.rsplit('endmodule', 1)[0] + "\n" + "endmodule"

def _remove_testbench(s: str) -> str:
    index = s.rfind('tb_module')
    if index == -1:
        index = s.find('testbench')
    if index != -1:
        s = _close_endmodule(s[:index])
    return s

def extract_verilog(raw: str, model_type: str) -> str:
    if model_type == 'deepseek':
        # 主截断：有 endmodulemodule 就只留第一个模块
        if len(raw.split('endmodulemodule', 1)) == 2:
            s = raw.split('endmodulemodule', 1)[0] + "\n" + "endmodule"
        else:
            s = _close_endmodule(raw)
        # top_module 包装层兜底
        if s.find('top_module') != -1:
            s = _close_endmodule(s.split('top_module', 1)[0])
    else:  # mistral
        s = _close_endmodule(raw)
    # 两种模型统一剔除 testbench
    s = _remove_testbench(s)
    return s


if __name__ == "__main__":
    clean = "module fa(input a,b,output s); assign s=a^b; endmodule"
    with_tb = clean + "\nmodule tb_module(); initial $display(); endmodule\n"
    deepseek_style = (
        "module fa(input a,b,output s); assign s=a^b; endmodule"
        "module second(input x); assign x=1; endmodule\n"
    )

    print("【干净输出/mistral】结尾正确:",
          extract_verilog(clean, 'mistral').strip().endswith('endmodule'))
    print("【带 testbench/mistral】已剔 testbench:",
          not any(k in extract_verilog(with_tb, 'mistral') for k in ('tb_module', 'testbench')))
    print("【Deepseek 连写】只留第一个模块:",
          'second' not in extract_verilog(deepseek_style, 'deepseek'))
    print("【Deepseek 连写】若误用 mistral 分支会保留两个模块:",
          'second' in extract_verilog(deepseek_style, 'mistral'))
```

**预期结果**（基于对源码逻辑的推导）：

- 干净输出经 `mistral` 处理后以 `endmodule` 结尾 → `True`；
- 带 testbench 输出经处理后不含 `tb_module`/`testbench` → `True`；
- Deepseek 连写输出经 `deepseek` 分支处理后不含 `second` → `True`；
- **同一份 Deepseek 连写输出若误用 `mistral` 分支**，因为走的是 `rsplit`（保留到最后一个 `endmodule`），第二个模块 `second` 会被保留 → `True`。这最后一条专门用来验证「**选错模型分支会得到错误结果**」，体现区分 `model_type` 的必要性。

**验收标准**：四个断言全部为 `True`；把 `model_type` 传错时，至少「Deepseek 连写」那条会失败，说明分支选择不可省略。

---

## 6. 本讲小结

- 模型的原始输出 `s_full` **不能直接当 Verilog 用**：可能含尾随噪声、testbench，Deepseek 版甚至会连写多个模块且不会自动停止。
- 通用截断地基是 `s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"`——取**最后一个** `endmodule` 之前的内容、补一个干净的 `endmodule`；这是 Mistral 版的主截断。
- testbench 剔除用「关键字定位」：先 `rfind('tb_module')`、再 `find('testbench')` 兜底，命中即砍掉该位置之后内容并重新闭合 `endmodule`。
- Deepseek 版的**唯一本质差异**在主截断：检测到连写关键字 `endmodulemodule` 时改用 `split`（从左切）只留第一个模块，并额外用 `top_module` 兜底；这与 Mistral 用 `rsplit`（保留到最后一个）相反。
- **切分方向编码了对模型行为的假设**——`split` 信「模型不停、第一个才对」，`rsplit` 信「模型会停、保留到最后一个」。这是本讲最重要的设计思想。
- 两个基准脚本默认启用 Mistral 版逻辑，Deepseek 版被整段注释保留；README 第 161–176 行是唯一**完整未注释**的 Deepseek 参考实现。

---

## 7. 下一步学习建议

- 想看这套后处理**被放在完整的基准推理主循环里**如何工作，请读 **u2-l8《基准评测推理脚本（VerilogEval 与 RTLLM）》**，它会讲清 `argparse` 参数、`description+prompt` 拼接、`n` 个候选重复采样，以及 RTLLM 如何用 `design_list` 关键字把结果落盘成 `.v` 文件。
- 想了解**为什么 Mistral 会自动停止、Deepseek 不会**（涉及训练时的 `eos_token` 与 `Response[-1]` 拼接），请读 **u2-l7《mle.py 标准监督微调》** 中关于 `eos_token` 和标签掩码的部分。
- 想从**模型部署**角度理解为什么有 GPTQ/GGUF 量化版（以及它们是否影响这套字符串后处理——答案是：不影响，后处理与量化无关），请读 **u3-l5《量化推理与二次开发扩展》**。
- 建议动手：在你的 `extract_verilog` 里加一个 `strict=True` 开关——当输出里**既没有** `endmodule` 也**没有** `endmodulemodule` 时抛出警告而不是默默返回原串，体会「**健壮的后处理应当对异常输出显式报错**」这一工程原则。
