# WAV 音频文件读写（wavfile.py）

## 1. 本讲目标

本讲深入 `scipy/io/wavfile.py` 的源码，读完之后你应当能够：

- 画出 WAV 文件「RIFF 外壳 + 一连串 chunk」的二进制骨架，并解释 `RIFF / RIFX / RF64` 三种魔数的区别。
- 读懂 `read` 如何像状态机一样逐 chunk 分发，以及 `write` 如何只凭一个 NumPy 数组反推出 WAV 的全部编码参数。
- 解释 `WAVE_FORMAT` 枚举、`KNOWN_WAVE_FORMATS` 集合，以及 PCM / IEEE_FLOAT 两种采样格式如何决定读回数据的 numpy dtype。
- 说明 `SeekEmulatingReader` 为什么存在：它如何在「不支持随机定位」的流（如管道）上用「向前读」来模拟 seek，并触发 `np.frombuffer` 回退路径。

本讲是 u1-l3「快速上手」的进阶版：u1-l3 只让你跑通 round-trip，本讲要拆开 WAV 这个黑盒，看清读和写各自是怎么逐字节工作的。

## 2. 前置知识

- **chunk（数据块）**：一种「先写 4 字节 ID、再写 4 字节长度、然后写长度字节数据」的自描述容器单位。WAV、AVI、RIFF 系列格式都用它。同一个文件里可以塞很多 chunk，解析器靠 ID 决定如何处理每一个。
- **字节序（endianness）**：多字节整数在内存中的存放顺序。`RIFF` 表示小端（little-endian，低位在前），`RIFX` 表示大端（big-endian，高位在前）。Python 里用 `struct` 模块的格式字符 `'<'` / `'>'` 控制。
- **采样格式（sample format）**：一段音频本质是「等间隔采样的整数或浮点数序列」。PCM（Pulse-Code Modulation）存原始整数样本；IEEE_FLOAT 存 ±1.0 范围内的浮点样本。8 位 PCM 是无符号（`uint8`），其余位数是补码有符号整数。
- **声道（channel）**：单声道是 1-D 数组，立体声（双声道）或多声道是 2-D 数组，形状为 `(采样数, 声道数)`。
- **`struct` 模块**：Python 标准库里按格式字符串打包/解包二进制数据的工具，例如 `struct.unpack('<HHIIHH', b)` 一次解出 6 个小端整数。
- **可定位流（seekable stream）**：支持 `seek()` 跳到任意位置的文件对象。普通文件可定位；管道、网络流、某些包装流不可定位。`wavfile.read` 既支持磁盘文件，也支持传入已打开的文件对象。

如果你对 NumPy dtype（`int16` / `uint8` / `float32` 等）还不熟，建议先回顾 u1-l3。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，但会触及它内部不同的职责分层：

| 源码位置 | 作用 |
| --- | --- |
| [wavfile.py:L37-L75](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L37-L75) | `SeekEmulatingReader`：把不可定位的流包装成「只能向前 seek」的流，让 `read` 也能工作。 |
| [wavfile.py:L78-L355](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L78-L355) | `WAVE_FORMAT` 枚举与 `KNOWN_WAVE_FORMATS`：列出所有已知采样编码标签，但只真正支持其中两种。 |
| [wavfile.py:L548-L612](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L548-L612) | 辅助函数：`_skip_unknown_chunk`、`_read_riff_chunk`、`_handle_pad_byte`，构成 chunk 遍历的基础设施。 |
| [wavfile.py:L368-L545](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L368-L545) | `_read_fmt_chunk` 与 `_read_data_chunk`：两个最核心的子 chunk 解析器。 |
| [wavfile.py:L615-L786](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L615-L786) | `read`：对外暴露的读入口，组织整个 chunk 循环。 |
| [wavfile.py:L789-L944](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L789-L944) | `write` 与 `_array_tofile`：对外暴露的写入口，从数组反推 WAV 参数并落盘。 |

> 说明：所有永久链接的 `HEAD` 固定为 `de190e7fde9d3d34400dbfe1eeacc9fc6d29cede`，与本讲生成时的仓库状态一致。

## 4. 核心概念与源码讲解

### 4.1 RIFF/chunk 骨架与 WAVE_FORMAT 采样格式

#### 4.1.1 概念说明

WAV 不是「扁平的一段音频」，而是一个 **RIFF 容器**：最外层是 12 字节的 RIFF 头（`RIFF` + 4 字节文件大小 + `WAVE`），里面再串接若干个 **chunk**。常见的 chunk 有：

- `fmt `（注意末尾有个空格）：描述采样格式、声道数、采样率等「编码参数」。
- `data`：真正的音频样本数据。
- `fact`：非 PCM 格式（如 float）常带的「样本总数」信息，SciPy 读取时直接跳过。
- `LIST`、`JUNK`、`Fake`：元数据或对齐填充 chunk，读取时跳过。

每个 chunk 的二进制布局固定为：

```
[ 4 字节 chunk ID ][ 4 字节 data 大小 N ][ N 字节数据 ][ 可能 1 字节 pad ]
```

「pad 字节」规则来自 RIFF 规范：当 `N` 是奇数时，要在数据末尾补一个 `0x00` 字节，使下一个 chunk 对齐到偶数地址。读和写都必须处理它，否则后续所有 chunk 的位置都会错位。

`WAVE_FORMAT` 则是一个枚举，列出 RIFF 规范里所有「采样编码标签」（wFormatTag）。但 SciPy **并不打算实现一个通用解码器**，它只支持最朴素的无压缩格式，所以代码里用一个小集合 `KNOWN_WAVE_FORMATS` 圈定真正能处理的两种。

#### 4.1.2 核心流程

读取 RIFF 外壳的伪流程：

1. 读 4 字节魔数，判断 `RIFF` / `RIFX` / `RF64`，由此确定字节序与是否 64 位。
2. 读 4 字节文件总大小（`RF64` 例外，真实大小在 `ds64` chunk 里）。
3. 读 4 字节 form 类型，必须是 `WAVE`，否则报错。
4. 之后进入 chunk 循环（详见 4.2）。

编码标签的处理流程：

- 读 `fmt ` chunk 拿到 `format_tag`。
- 若 `format_tag` 不在 `KNOWN_WAVE_FORMATS` 里，调用 `_raise_bad_format` 抛出 `ValueError`，并友好地把支持列表打印出来。
- `EXTENSIBLE`（`0xFFFE`）是一种「把真实标签藏进 GUID」的扩展头，代码会从 GUID 里解出真实的 `format_tag` 再判断。

字段之间存在约束关系，读时会校验：

\[
\text{bytes\_per\_second} = f_s \times \text{block\_align}
\]

其中 \(f_s\) 是采样率，`block_align` 是「每个采样帧（含所有声道）的字节数」。

#### 4.1.3 源码精读

**RIFF 外壳解析**——判断三种魔数并确定字节序：[wavfile.py:L565-L605](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L565-L605)。这段代码先按 `b'RIFF'` / `b'RIFX'` / `b'RF64'` 分流，分别设置 `is_big_endian`、`is_rf64` 和用于后续解包的 `struct` 格式字符（`<I` 小端 4 字节、`>I` 大端、`<Q` 8 字节）。`RF64` 是为超过 4 GiB 的大文件设计的 64 位扩展：它的「文件大小」字段填 `0xFFFFFFFF` 占位，真实大小放在紧跟的 `ds64` chunk 中，代码在第 593-600 行校验并跳过 `ds64`。

**pad 字节处理**——chunk 对齐：[wavfile.py:L608-L612](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L608-L612)。只有当 chunk 大小为奇数时才 `seek(1, 1)` 跳过一个字节。每个 chunk 读完都要调一次它。

**跳过未知 chunk**：[wavfile.py:L548-L562](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L548-L562)。读出 chunk 大小后直接 `seek` 跳过，并同样处理 pad 字节。注意第 559 行的守卫：只有真的读到数据时才 `unpack`，避免文件末尾空读触发异常。

**WAVE_FORMAT 枚举与支持集合**：[wavfile.py:L78-L355](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L78-L355)。这是一个继承自 `IntEnum` 的枚举，罗列了数百种编码标签（来源是 Windows SDK 的 `mmreg.h`）。关键是两个值：`PCM = 0x0001`（第 86 行）和 `IEEE_FLOAT = 0x0003`（第 88 行），以及 `EXTENSIBLE = 0xFFFE`（第 351 行）。紧随其后的 [L355](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L355) 定义 `KNOWN_WAVE_FORMATS = {WAVE_FORMAT.PCM, WAVE_FORMAT.IEEE_FLOAT}`，把「知道」和「支持」明确分开。

**报错助手**：[wavfile.py:L358-L365](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L358-L365)。遇到不支持的标签时，先尝试用 `WAVE_FORMAT(format_tag).name` 把数字翻译成可读名字（如 `MULAW`），翻译不出就打印十六进制，再列出支持列表，错误信息对用户很友好。

#### 4.1.4 代码实践

**目标**：亲手验证 `WAVE_FORMAT` 枚举的取值与 `KNOWN_WAVE_FORMATS` 的范围。

```python
# 示例代码
from scipy.io.wavfile import WAVE_FORMAT, KNOWN_WAVE_FORMATS

print("PCM 的数值是", int(WAVE_FORMAT.PCM))          # 期望 1
print("IEEE_FLOAT 的数值是", int(WAVE_FORMAT.IEEE_FLOAT))  # 期望 3
print("真正支持的格式:", [f.name for f in KNOWN_WAVE_FORMATS])
# 期望 ['PCM', 'IEEE_FLOAT']
print("0x0007 对应的名字:", WAVE_FORMAT(0x0007).name)  # 期望 MULAW（μ-law）
```

- **操作步骤**：保存为脚本并运行。
- **需要观察的现象**：`KNOWN_WAVE_FORMATS` 只有两个成员，远小于枚举总数；`WAVE_FORMAT(0x0007)` 能反查出名字 `MULAW`。
- **预期结果**：证明 SciPy「认识」几百种格式名，但只「支持」两种无压缩格式，其余只用于把错误信息翻译成可读名字。
- 如果无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WAVE_FORMAT` 里 `IMA_ADPCM` 和 `DVI_ADPCM` 是同一个值 `0x0011`？

> **参考答案**：见 [wavfile.py:L98-L99](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L98-L99)，两者在 RIFF 规范里共用同一个 wFormatTag `0x0011`，只是历史上有两个名字。`IntEnum` 允许别名（后定义的成为别名），所以这里能同时列出而不报错。

**练习 2**：`_handle_pad_byte` 为什么要对「奇数大小」补一个字节？如果不处理会怎样？

> **参考答案**：RIFF 规范要求每个 chunk 对齐到偶数字节边界。如果不跳过这个 pad 字节，下一个 chunk 的 ID 就会从错误的位置读起，导致 `chunk_id` 变成乱码，整个解析错位失败。

---

### 4.2 read 主流程：逐 chunk 分发

#### 4.2.1 概念说明

`read` 是对外暴露的读入口。它的设计哲学是：**把 WAV 当成一个 chunk 流来顺序遍历**，而不是「先读固定 44 字节头再读数据」。这样做的好处是能容忍各种非标准 chunk 顺序、多余元数据 chunk、提前结束等真实世界里常见的「脏 WAV」。

它内部维护两个布尔状态：`fmt_chunk_received` 和 `data_chunk_received`，用来跟踪关键 chunk 是否已经出现，并在异常情况下给出合理的处理（数据已读完就警告并优雅退出，否则抛错）。

`read` 还要兼顾两类输入：磁盘文件路径，以及已经打开的文件对象（包括不可定位的流）。后者正是 `SeekEmulatingReader` 介入的时机（详见 4.5）。

#### 4.2.2 核心流程

`read` 的主循环伪代码：

```
打开文件（或使用传入的文件对象）
若流不可定位 → 用 SeekEmulatingReader 包装
解析 RIFF 外壳 → 得到 file_size, is_big_endian, is_rf64, rf64_chunk_size
while 当前位置 < file_size:
    读 4 字节 chunk_id
    根据 chunk_id 分发:
        b'fmt '  → 调 _read_fmt_chunk，记下 format_tag/channels/fs/bit_depth/block_align
        b'data'  → 必须 fmt 已就绪；调 _read_data_chunk 读样本
        b'fact'  → 跳过
        b'LIST'  → 跳过
        b'JUNK' / b'Fake' → 静默跳过（对齐填充）
        其他     → 发 WavFileWarning 并跳过
返回 (fs, data)
```

关键约束：**`data` chunk 必须出现在 `fmt` chunk 之后**，否则不知道样本的编码与位深，无法解析。

#### 4.2.3 源码精读

**入口与流包装**：[wavfile.py:L717-L724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L717-L724)。第 717 行用 `hasattr(filename, 'read')` 区分「文件对象」与「路径」；第 723-724 行用海象运算符 `was_seekable := fid.seekable()` 判断，若不可定位就用 `SeekEmulatingReader` 包装。

**chunk 循环主体**：[wavfile.py:L730-L777](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L730-L777)。这是整个读取的中枢。逐 chunk 读取 ID 后用一串 `if/elif` 分发。注意几处细节：
- 第 734-751 行处理「读到空或不完整 chunk」：如果数据 chunk 已成功读取，就降级为 `WavFileWarning` 优雅收尾；否则抛 `ValueError`。
- 第 759 行 `b'fact'`、第 768 行 `b'LIST'`、第 771 行 `b'JUNK'`/`b'Fake'` 都调用 `_skip_unknown_chunk` 跳过，但 `JUNK`/`Fake` **不发警告**（它们本就是对齐用的合法 chunk）。
- 第 774-777 行：真正未知的 chunk 会发 `WavFileWarning` 再跳过。

**返回与清理**：[wavfile.py:L778-L786](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L778-L786)。`finally` 块负责：若 `read` 自己打开的文件就关闭它；若传入的是可定位文件对象，则 `seek(0)` 回卷，方便调用方继续使用原始流。最后返回 `(fs, data)`——注意采样率在前，数据在后。

#### 4.2.4 代码实践

**目标**：用一个故意「带额外 chunk」的真实测试文件，观察 `read` 如何容忍非标准 chunk。

```python
# 示例代码
import warnings
from scipy.io import wavfile

# 这个文件名里带 "extra"，表示含有非标准/额外 chunk
fn = ("path/to/scipy/io/tests/data/"
      "test-1234Hz-le-1ch-10S-20bit-extra.wav")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    rate, data = wavfile.read(fn)
    print("采样率:", rate)
    print("数据形状:", data.shape, "dtype:", data.dtype)
    for warning in w:
        print("警告:", warning.message)
```

- **操作步骤**：把 `fn` 替换为你本地仓库 `scipy/io/tests/data/` 下的真实路径后运行。
- **需要观察的现象**：即使文件含额外 chunk，`read` 仍能返回正确的采样率与数据；若额外 chunk 是未知类型，会打印一条 `Chunk (non-data) not understood, skipping it.` 警告。
- **预期结果**：`rate == 1234`，`data` 是 20-bit 数据（在 numpy 里被提升为 int32 容器），形状为 `(10,)`。
- 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果 `data` chunk 出现在 `fmt` chunk **之前**，`read` 会怎样？

> **参考答案**：见 [wavfile.py:L762-L764](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L762-L764)，此时 `fmt_chunk_received` 仍为 `False`，代码抛出 `ValueError("No fmt chunk before data")`。因为解析样本必须先知道编码参数。

**练习 2**：为什么 `b'JUNK'` chunk 跳过时**不发警告**，而其他未知 chunk 会发 `WavFileWarning`？

> **参考答案**：见 [wavfile.py:L771-L777](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L771-L777)。`JUNK` 和 `Fake` 是 RIFF 规范里专门用于「对齐填充」的合法 chunk，存在是正常的；而真正未知的 chunk 可能意味着文件损坏或编码扩展，值得用警告提醒用户。

---

### 4.3 fmt 与 data chunk 解析：编码参数到 numpy dtype

#### 4.3.1 概念说明

`fmt ` chunk 描述「这段音频是怎么编码的」，`data` chunk 装着「按这种编码排布的原始字节」。`_read_data_chunk` 的核心任务，就是把 `format_tag`、`bit_depth`、`bytes_per_sample`、`channels` 这几个参数，翻译成一个合适的 numpy dtype，再把原始字节解释成数组。

这里有几个容易踩坑的点：

1. **8 位 PCM 是无符号，其余位数是有符号**：WAV 规范的特殊规定，代码用 `u1`（unsigned）处理 8 位及以下，用有符号类型处理更高位。
2. **容器大小可能不等于位深**：例如「24-bit packed」可能用 3 字节容器，也可能塞进 4 字节容器。`bytes_per_sample = block_align // channels` 取的是「容器大小」，而不是位深除以 8。
3. **3/5/6/7 字节容器没有原生 numpy dtype**：代码先按原始字节读入（dtype `V1`），再手工重排进 int32/int64 容器，并按字节序左对齐。
4. **多声道要 reshape**：读出来是 1-D，再 `reshape(-1, channels)` 变成 `(采样数, 声道数)`。

#### 4.3.2 核心流程

`_read_fmt_chunk` 流程：

```
按字节序读 4 字节 size
若 size < 16 → 报错（结构不合规）
读 16 字节核心字段: (format_tag, channels, fs, bytes_per_second, block_align, bit_depth)
若 format_tag == EXTENSIBLE 且 size 足够 → 从 GUID 解出真实 format_tag
若 format_tag 不在 KNOWN_WAVE_FORMATS → _raise_bad_format
跳过 size 多出的字节 + pad 字节
若 PCM → 校验 bytes_per_second == fs * block_align
返回 7 元组
```

`_read_data_chunk` 的 dtype 决策表（PCM 分支）：

| bit_depth / 容器 | numpy dtype |
| --- | --- |
| 1–8 位 | `u1`（无符号 1 字节） |
| 容器为 3/5/6/7 字节 | `V1`（先按原始字节读，再重排） |
| 其余 ≤ 64 位 | `{fmt}i{bytes_per_sample}`（有符号，如 `<i2`、`<i4`） |

IEEE_FLOAT 分支只接受 32 或 64 位，dtype 为 `{fmt}f{bytes_per_sample}`。

样本数计算：

\[
n_{\text{samples}} = \frac{\text{size}}{\text{bytes\_per\_sample}}
\]

读出后若 `channels > 1`，reshape 成 `(n_samples // channels` 行 × `channels` 列）。

#### 4.3.3 源码精读

**`_read_fmt_chunk` 字段解包**：[wavfile.py:L368-L444](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L368-L444)。第 401 行 `struct.unpack(fmt+'HHIIHH', fid.read(16))` 一次读出 6 个核心字段：`format_tag(H)`、`channels(H)`、`fs(I)`、`bytes_per_second(I)`、`block_align(H)`、`bit_depth(H)`。第 406-423 行处理 `EXTENSIBLE` 扩展头：从 GUID 模板 `{XXXXXXXX-0000-0010-8000-00AA00389B71}` 里抽出前 4 字节作为真实 `format_tag`，并按字节序处理 GUID 尾部。第 435-441 行对 PCM 做一致性校验：`bytes_per_second` 必须等于 `fs * block_align`。

**`_read_data_chunk` 的 dtype 决策**：[wavfile.py:L491-L510](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L491-L510)。这段就是上面决策表的代码实现。注意第 492-493 行：`1 <= bit_depth <= 8` 用 `'u1'`；第 494-496 行：容器为 3/5/6/7 字节时用 `'V1'` 先按原始字节装载。

**3/5/6/7 字节容器的重排**：[wavfile.py:L521-L530](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L521-L530）。这是处理 24-bit（最常见）等非标准容器大小的精妙之处：先建一个 `int32`（3 字节）或 `int64`（5/6/7 字节）的「大容器」数组，再按字节序把原始字节左对齐塞进去，最后用 `.view(dt)` 重新解释。这样 24-bit 数据就变成 int32（最高有效字节对齐），与 WAV 的「left-justified」格式一致。

**优先 np.fromfile，回退 np.frombuffer**：[wavfile.py:L512-L519](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L512-L519)。`np.fromfile` 更快（直接走 C 文件描述符），但它要求流「像 C 文件」（可 seek、可 flush）。当流是 `SeekEmulatingReader` 或 `io.BytesIO` 时，`np.fromfile` 会抛 `io.UnsupportedOperation`，代码就回退到 `np.frombuffer(fid.read(size), ...)`。这条回退路径正是 `SeekEmulatingReader.flush()` 故意抛异常（见 4.5）的目的。

**多声道 reshape**：[wavfile.py:L543-L545](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L543-L545)。`data.reshape(-1, channels)` 把 1-D 样本流变成 2-D `(帧数, 声道数)`。

#### 4.3.4 代码实践

**目标**：用不同位深的测试文件，对照 `read` 返回的 dtype 与位深的关系。

```python
# 示例代码
from scipy.io import wavfile

files = {
    "8-bit unsigned": "test-8000Hz-le-2ch-1byteu.wav",
    "24-bit":         "test-8000Hz-le-3ch-5S-24bit.wav",
    "32-bit float":   "test-44100Hz-2ch-32bit-float-le.wav",
}
base = "path/to/scipy/io/tests/data/"
for label, fn in files.items():
    rate, data = wavfile.read(base + fn)
    print(f"{label:18s} → rate={rate}, shape={data.shape}, dtype={data.dtype}")
```

- **操作步骤**：替换 `base` 为真实路径后运行。
- **需要观察的现象**：
  - 8-bit 文件 → `dtype=uint8`
  - 24-bit 文件 → `dtype=int32`（被提升到 4 字节容器）
  - 32-bit float 文件 → `dtype=float32`
- **预期结果**：印证 dtype 决策表——8 位无符号、24 位进 int32 容器、float 进 float32。立体声/多声道文件 `shape` 第二维是声道数。
- 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 8-bit PCM 用 `uint8`（无符号），而 16-bit PCM 用 `int16`（有符号）？

> **参考答案**：这是 WAV 规范的历史约定。见 [wavfile.py:L492-L493](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L492-L493)，代码用 `1 <= bit_depth <= 8` 判定走 `'u1'` 分支。原因之一是早期 8 位音频硬件按无符号方式处理。

**练习 2**：`bytes_per_sample` 用的是 `block_align // channels`，为什么不直接用 `bit_depth // 8`？

> **参考答案**：见 [wavfile.py:L488](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L488)。`block_align` 反映「每个采样帧实际占用的字节数」，可能与 `bit_depth // 8` 不一致——例如「24-bit 数据塞在 4 字节容器」或「20-bit 数据用 3 字节容器」。用 `block_align // channels` 才能正确还原真实内存布局，函数 docstring（[L454-L471](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L454-L471)）列举了 Adobe Audition 等真实案例。

---

### 4.4 write：从 NumPy 数组反推 WAV 参数

#### 4.4.1 概念说明

`write` 的输入极简：一个文件名、一个采样率、一个 NumPy 数组。它要凭这三样东西**反推出** WAV 需要的全部编码参数：

- 数组 dtype 的 `kind`（`f` 浮点 → `IEEE_FLOAT`，`i`/`u` 整型 → `PCM`）。
- 数组维度决定声道数（1-D 单声道，2-D 多声道，列数即声道数）。
- `dtype.itemsize * 8` 决定位深。
- 采样率、声道数、位深三者算出 `bytes_per_second` 和 `block_align`。

`write` 还处理两个进阶情形：非 PCM 文件需要多写一个 `cbSize` 字段和 `fact` chunk；当文件超过 4 GiB 时，需要改用 `RF64` 64 位格式。最后，由于写头时还不知道最终文件大小，它会**先占位、写完数据再回填**文件大小字段。

#### 4.4.2 核心流程

```
打开输出文件
校验 dtype 在 allowed_dtypes 内（float32/64, uint8, int16/32/64）
写 RIFF 外壳（大小先占位 0）
根据 dtype.kind 决定 format_tag（float→IEEE_FLOAT，否则 PCM）
根据 ndim 决定 channels；itemsize*8 决定 bit_depth
算 bytes_per_second = fs * (bit_depth//8) * channels；block_align = channels*(bit_depth//8)
打包 fmt chunk（<HHIIHH）；非 PCM 追加 cbSize=\x00\x00
若预计文件 > 4GiB → 改写 RF64 外壳 + ds64 chunk
非 PCM → 追加 fact chunk
写 data chunk 头 + 样本数据（按需 byteswap 到小端）
回填文件大小到偏移 4（RF64 则到偏移 20）
```

注意 `write` **总是写小端**：若输入数组是大端或当前机器是大端，会先 `byteswap` 再落盘（[L920-L923](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L920-L923)）。这对应 RIFF（小端）魔数。

#### 4.4.3 源码精读

**dtype 校验与 format_tag 决策**：[wavfile.py:L850-L878](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L850-L878)。第 852-855 行限定 `allowed_dtypes`，不在列表里的（如 `int8`、`float16`）直接拒绝。第 865-868 行用 `dkind == 'f'` 区分浮点与整型，决定 `format_tag`。第 877 行 `struct.pack('<HHIIHH', format_tag, channels, fs, bytes_per_second, block_align, bit_depth)` 与 `_read_fmt_chunk` 的解包格式严格对应——读写对称是格式正确性的保证。

**RF64 回退**：[wavfile.py:L886-L906](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L886-L906)。第 888-889 行预估总大小，若超过 `0xFFFFFFFF`（4 GiB）就丢弃已构建的小端头，改写 `RF64` 外壳并附加 `ds64` chunk（含真实文件大小、数据大小、样本数）。

**fact chunk**：[wavfile.py:L909-L911](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L909-L911)。非 PCM（即浮点）文件按规范写 `fact` chunk，内容是样本帧数 `data.shape[0]`。

**写数据 + 回填大小**：[wavfile.py:L915-L933](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L915-L933）。先写 `data` chunk 头（大小用 `min(data.nbytes, 4294967295)` 限制），再写样本。最后 `fid.tell()` 取得真实大小，`seek(4)` 回到文件头，把「文件大小 - 8」写回 RIFF 的 size 字段（RF64 则 `seek(20)` 写 8 字节）。这种「占位 + 回填」是二进制写文件的常见手法。

**底层写数组**：[wavfile.py:L942-L944](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L942-L944)。`_array_tofile` 用 `data.ravel().view('b').data` 把数组展平成连续字节缓冲再写，绕开了 `np.tofile` 对文件描述符的依赖（这样也能写 `io.BytesIO`）。

#### 4.4.4 代码实践

**目标**：分别写 int16 PCM 与 float32 两种文件，比较 `fmt ` chunk 大小（PCM 是 16 字节，float 因多了 cbSize 是 18 字节）。

```python
# 示例代码
import numpy as np
import struct
from scipy.io import wavfile

rate = 8000
t = np.linspace(0, 1, rate, endpoint=False)

# int16 PCM
pcm = (np.iinfo(np.int16).max * np.sin(2*np.pi*440*t)).astype(np.int16)
wavfile.write("pcm16.wav", rate, pcm)

# float32
flt = np.sin(2*np.pi*440*t).astype(np.float32)
wavfile.write("float32.wav", rate, flt)

for fn in ["pcm16.wav", "float32.wav"]:
    with open(fn, "rb") as f:
        head = f.read(40)
    fmt_size = struct.unpack('<I', head[16:20])[0]
    tag = struct.unpack('<H', head[20:22])[0]
    print(f"{fn}: fmt chunk size={fmt_size}, format_tag={tag}")
```

- **操作步骤**：运行脚本，它会在当前目录生成两个 WAV 并打印它们的 fmt chunk 大小。
- **需要观察的现象**：`pcm16.wav` 的 fmt chunk size = 16、format_tag = 1（PCM）；`float32.wav` 的 fmt chunk size = 18（多了 2 字节 cbSize）、format_tag = 3（IEEE_FLOAT）。
- **预期结果**：印证 [L879-L881](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L879-L881)——非 PCM 文件会追加 `b'\x00\x00'` 作为 cbSize，使 fmt chunk 从 16 字节变 18 字节。
- 标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `write` 在写完数据后要 `seek(4)` 再写一次？

> **参考答案**：见 [wavfile.py:L927-L933](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L927-L933)。写头时还不知道最终文件大小，所以 RIFF size 字段先填 0（第 860 行 `b'\x00\x00\x00\x00'`）；写完所有数据后才知道真实大小，于是回到偏移 4 把它补上。这是「占位 + 回填」模式。

**练习 2**：若传入一个 `int8` 数组，`write` 会怎样？

> **参考答案**：见 [wavfile.py:L852-L855](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L852-L855)。`int8` 不在 `allowed_dtypes`（`['float32','float64','uint8','int16','int32','int64']`）里，会抛 `ValueError("Unsupported data type 'int8'")`。注意 8 位只接受**无符号** `uint8`。

---

### 4.5 SeekEmulatingReader：在不可定位的流上模拟 seek

#### 4.5.1 概念说明

很多文件读取算法需要 `seek`（随机定位）——比如 `np.fromfile`、回填大小、跳过未知 chunk。但并不是所有输入流都支持 `seek`：管道（pipe）、某些网络流、被包装过的流都可能不可定位。

`wavfile.read` 希望同时支持「磁盘文件」和「任意文件对象」。当传入的流不可定位时，它用一个轻量包装类 `SeekEmulatingReader` 把流包装起来。这个类的策略很朴素：**只支持「向前」的 seek——把要跳过的字节直接读出来丢掉**；遇到无法模拟的 seek（比如向后跳）就抛 `io.UnsupportedOperation`。

它还有一个小但关键的设计：故意让 `flush()` 抛 `io.UnsupportedOperation`。这会迫使 `_read_data_chunk` 在调用 `np.fromfile` 失败后，回退到 `np.frombuffer(fid.read(size))` 路径，从而兼容不可定位流。

#### 4.5.2 核心流程

`SeekEmulatingReader` 维护一个 `pos` 计数器，记录「已经读到第几字节」。每次操作：

- `read(size)`：委托给底层流读取，并把读到的字节数累加进 `pos`。
- `seek(offset, whence)`：用 `match/case`（结构化模式匹配）判断能否模拟：
  - `SEEK_SET` 且 `offset >= pos`：读掉 `offset - pos` 字节（等价于前移）。
  - `SEEK_CUR` 且 `offset >= 0`：读掉 `offset` 字节。
  - `SEEK_END` 且 `offset == 0`：一直读到流末尾。
  - 其他（向后跳等）：抛 `io.UnsupportedOperation`。
- `tell()`：返回 `pos`。
- `flush()`：故意抛 `io.UnsupportedOperation`（触发 `np.frombuffer` 回退）。

整条回退链路：

```
read() 发现流不可 seek
  → 用 SeekEmulatingReader 包装
_read_data_chunk 调 np.fromfile(fid)
  → np.fromfile 调 fid.flush() → 抛 io.UnsupportedOperation
  → except 分支回退 np.frombuffer(fid.read(size), dtype)
  → SeekEmulatingReader.read() 向前读并累加 pos
```

#### 4.5.3 源码精读

**SeekEmulatingReader 定义**：[wavfile.py:L37-L75](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L37-L75)。第 48-51 行的 `read` 累加 `pos`。第 53-64 行的 `seek` 用 `match whence:` 做结构化匹配——注意它只接受「能通过向前读来达成」的 seek，其余一律抛 `io.UnsupportedOperation`。第 74-75 行的 `flush` 故意抛异常。

**何时启用包装**：[wavfile.py:L723-L724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L723-L724)。`read` 在打开流后立即判断 `fid.seekable()`，为 `False` 才包装。

**flush 抛异常如何触发回退**：[wavfile.py:L512-L519](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L512-L519)。`np.fromfile` 内部会调用 `fid.flush()`，对 `SeekEmulatingReader` 来说就是 `io.UnsupportedOperation`；`except` 捕获后改用 `np.frombuffer(fid.read(size), dtype=dtype)`。类 docstring（[L72-L74](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L72-L74)）明确写出了这一约定。

> 重要细节：`io.BytesIO` 虽然可 `seek`，但它没有真正的 C 文件描述符，`np.fromfile` 同样会抛 `io.UnsupportedOperation`，于是也走 `np.frombuffer` 回退——这就是为什么 `read` 文档里说「传入 `io.BytesIO` 时返回的数据不可写」（`np.frombuffer` 产生只读视图）。

#### 4.5.4 代码实践

**目标**：用 `io.BytesIO`（一个可 seek 但无 C 文件描述符的流）喂给 `read`，观察它走 `np.frombuffer` 回退路径，返回只读数组。

```python
# 示例代码
import io
import numpy as np
from scipy.io import wavfile

# 先生成一段合法的 WAV 字节
rate = 8000
t = np.linspace(0, 0.1, rate // 10, endpoint=False)
data = (np.iinfo(np.int16).max * np.sin(2*np.pi*440*t)).astype(np.int16)
wavfile.write("tmp.wav", rate, data)

# 把文件内容读进 BytesIO 再交给 read
buf = io.BytesIO(open("tmp.wav", "rb").read())
r2, d2 = wavfile.read(buf)
print("采样率:", r2, " dtype:", d2.dtype, " 可写:", d2.flags.writeable)
```

- **操作步骤**：运行脚本。
- **需要观察的现象**：`read` 能成功从 `BytesIO` 读出数据，但 `d2.flags.writeable` 为 `False`。
- **预期结果**：因为 `BytesIO` 无 C 文件描述符，`np.fromfile` 抛 `io.UnsupportedOperation`，回退到 `np.frombuffer`，产生只读视图，印证 [L517-L519](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L517-L519) 的回退分支。
- 标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`SeekEmulatingReader` 为什么**不实现**「向后 seek」？

> **参考答案**：见 [wavfile.py:L61-L63](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L61-L63)。底层流不可定位，无法回到已经读过的位置；唯一能模拟的是「向前读」来推进位置。向后 seek 需要缓冲全部已读数据或重开流，代价大且 `wavfile.read` 的解析逻辑（顺序遍历 chunk）基本只需要前移，所以作者只实现了最小必要子集（类 docstring 明确说 "implements only the minimum necessary"）。

**练习 2**：如果底层流是磁盘文件（`seekable()` 为真），`read` 会用 `SeekEmulatingReader` 吗？

> **参考答案**：不会。见 [wavfile.py:L723-L724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L723-L724)，只有 `fid.seekable()` 为 `False` 时才包装。磁盘文件直接用原生 `seek`，`np.fromfile` 也能走快速的 C 文件描述符路径。

---

## 5. 综合实践

把本讲的「读、写、chunk 结构、dtype 映射」串起来，完成下面的任务。

### 任务：立体声左右声道交换 + 手工解析 44 字节 RIFF 头

**步骤 1：生成一个标准立体声 int16 WAV。** 用 `write` 写出的 int16 PCM 文件，其头部恰好是 44 字节（`RIFF` 12 字节 + `fmt ` 16 字节数据/24 字节含头 + `data` 8 字节头），非常适合手工解析。

```python
# 示例代码
import struct
import numpy as np
from scipy.io import wavfile

rate = 44100
t = np.linspace(0, 1, rate, endpoint=False)
# 左声道 440Hz，右声道 880Hz，振幅不同以便区分
left = (np.iinfo(np.int16).max * 0.6 * np.sin(2*np.pi*440*t)).astype(np.int16)
right = (np.iinfo(np.int16).max * 0.3 * np.sin(2*np.pi*880*t)).astype(np.int16)
stereo = np.column_stack([left, right])   # shape (N, 2)
wavfile.write("stereo.wav", rate, stereo)
```

**步骤 2：读回，交换左右声道，写出新文件。**

```python
# 示例代码
r, data = wavfile.read("stereo.wav")
print("读回: rate =", r, ", shape =", data.shape, ", dtype =", data.dtype)
swapped = data[:, ::-1]   # 交换第 0、1 列（左右声道）
wavfile.write("stereo_swapped.wav", r, swapped)

# 验证：再读新文件，确认左右已互换
r2, data2 = wavfile.read("stereo_swapped.wav")
print("声道交换后左声道是否等于原右声道:",
      np.array_equal(data2[:, 0], data[:, 1]))
print("声道交换后右声道是否等于原左声道:",
      np.array_equal(data2[:, 1], data[:, 0]))
```

**步骤 3：用 `struct` 手工解析前 44 字节，与 `read` 返回的采样率对照。**

```python
# 示例代码
with open("stereo.wav", "rb") as f:
    raw = f.read(44)

# 一次性解出 12 个字段（共 44 字节）
(riff, size8, wave, fmt_id, fmt_sz,
 tag, channels, fs, bps, block_align, bit_depth,
 data_id, data_size) = struct.unpack('<4sI4s4sIHHIIHH4sI', raw)

print("RIFF 魔数      :", riff)        # b'RIFF'
print("WAVE 标记      :", wave)        # b'WAVE'
print("fmt chunk ID   :", fmt_id)      # b'fmt '
print("fmt chunk 大小 :", fmt_sz)      # 16
print("format_tag     :", tag, "(1=PCM)")  # 1
print("声道数         :", channels)    # 2
print("采样率         :", fs)          # 44100
print("block_align    :", block_align) # 4 = 2声道 * 2字节
print("位深           :", bit_depth)   # 16
print("data chunk ID  :", data_id)     # b'data'

# 与 read 返回值对照
print("read 返回采样率:", r, "，与手工解析一致？", r == fs)
print("read 返回声道数:", data.shape[1], "，与手工解析一致？", data.shape[1] == channels)
```

**预期结果**：
- `riff == b'RIFF'`、`wave == b'WAVE'`、`fmt_id == b'fmt '`、`data_id == b'data'`。
- `fmt_sz == 16`、`tag == 1`、`channels == 2`、`fs == 44100`、`block_align == 4`、`bit_depth == 16`。
- `read` 返回的采样率与手工解析的 `fs` 完全一致。
- 声道交换后的两个 `np.array_equal` 断言都为 `True`。

**思考延伸**：如果你改用 `float32` 立体声数据写文件，前 44 字节还能这样干净地解析吗？为什么？（提示：float 文件的 `fmt ` chunk 是 18 字节，且多出一个 `fact` chunk，data chunk 起始位置会后移——这正是 4.4 节看到的 cbSize 与 fact chunk 的影响。）

> 若在本地无法运行，相关结论请标注「待本地验证」。

## 6. 本讲小结

- WAV 是 **RIFF 容器**：`RIFF/RIFX/RF64` 三种魔数决定字节序与是否 64 位，内部由一连串「ID + 大小 + 数据 + 可能的 pad 字节」chunk 组成；`_read_riff_chunk` 与 `_handle_pad_byte` 是骨架。
- `WAVE_FORMAT` 枚举罗列数百种编码标签，但 `KNOWN_WAVE_FORMATS` 只圈定 `PCM` 与 `IEEE_FLOAT` 两种真正支持的格式；其余只用于把报错信息翻译成可读名字。
- `read` 用**逐 chunk 顺序遍历**而非固定偏移读头，靠 `fmt_chunk_received` / `data_chunk_received` 两个状态容忍非标准 chunk 顺序与脏文件。
- `_read_data_chunk` 把「format_tag + bit_depth + bytes_per_sample + channels」翻译成 numpy dtype：8 位无符号、24 位进 int32 容器、float 进 float32/64；优先 `np.fromfile`，失败回退 `np.frombuffer`。
- `write` 反过来从「dtype.kind + ndim + itemsize」反推全部 WAV 参数，采用「先占位、写完数据再 `seek` 回填文件大小」的手法，并在超过 4 GiB 时自动切到 RF64。
- `SeekEmulatingReader` 用「向前读丢掉」模拟 seek，故意让 `flush()` 抛异常以触发 `np.frombuffer` 回退，使 `read` 能兼容管道、`io.BytesIO` 等不可定位或无 C 文件描述符的流。

## 7. 下一步学习建议

- **横向对比另一个格式**：本讲是「二进制 chunk 容器」的典型代表。下一讲 u2-l2「Fortran 无格式顺序文件（FortranFile）」是另一种二进制结构（每条记录前后各有一个长度标记），读完会发现 RIFF chunk 与 Fortran record 在「自描述长度」思想上的异同。
- **继续深入横切主题**：若你对 `np.fromfile` / `np.frombuffer` / `mmap` 这些底层 I/O 路径感兴趣，可以直接跳到 u4-l4「健壮性、错误处理与安全性取舍」，那里专门讨论 mmap 的内存生命周期陷阱与各格式异常体系。
- **扩展阅读源码**：想看更多 chunk 跳过与字节序处理的例子，可以读 `_harwell_boeing/_fortran_format_parser.py`（u2-l6）和 `matlab/_miobase.py`（u3-l2），它们面对的是更复杂的二进制记录结构。
- **动手验证**：强烈建议把综合实践里的「声道交换 + 44 字节解析」跑一遍，亲手看到 `struct.unpack` 的输出与 `read` 返回值逐字段对齐，能极大巩固对 RIFF 结构的理解。
