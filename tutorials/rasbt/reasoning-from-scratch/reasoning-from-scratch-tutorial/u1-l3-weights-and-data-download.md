# u1-l3 权重与数据下载机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行解释 `reasoning_from_scratch/utils.py` 中 `download_file` 的完整下载流程：分块下载、进度显示、主 URL/备用 URL 双通道、以及「文件已完整存在则跳过」的缓存策略。
2. 解释 `_download_error_message` 如何把底层网络异常翻译成带排查指引的友好错误信息，特别是它对 SSL/代理类问题的自动识别。
3. 说出 `qwen3.py` 中三个下载入口的分工：`download_qwen3_small`（第 2~5 章用的 base/reasoning 权重与分词器）、`download_qwen3_grpo_checkpoints`（第 7 章 GRPO 训练检查点）、`download_qwen3_distill_checkpoints`（第 8 章蒸馏检查点）。
4. 独立完成：只下载 tokenizer 文件、验证二次运行命中缓存、以及用无效 URL 触发一次带 troubleshooting 链接的报错。

本讲不涉及任何模型计算，只讲「文件如何落到你的硬盘上」。但这条链路是后续每一章的第一行代码——上一讲 `generate_simple.py` 里加载的 `qwen3-0.6B-base.pth` 和 `tokenizer-base.json`，正是从这里来的。

## 2. 前置知识

- **HTTP 响应是流（stream）**：默认情况下 `requests.get()` 会把整个响应体一次性读进内存。对一个约 1.4 GB 的模型权重文件来说这很浪费。传入 `stream=True` 后，响应体不会立刻读取，而是可以按块（chunk）迭代读取，内存中任何时候只有一小块数据。
- **Content-Length 响应头**：服务器通常会在响应头里给出文件的总字节数。本项目用它做两件事：显示下载进度百分比、判断本地文件是否已经下载完整。
- **MiB 与 MB**：1 MiB = \( 1024 \times 1024 \) 字节（二进制），1 MB = \( 10^6 \) 字节（十进制）。源码中的分块大小 `1024 * 1024` 就是 1 MiB。
- **Path 与 urlparse**：`pathlib.Path` 用于跨平台拼接文件路径；`urllib.parse.urlparse("https://host/a/b/c.pth").path` 返回 `/a/b/c.pth`，再用 `.name` 取出文件名 `c.pth`——这就是「从 URL 推导本地文件名」的办法。
- **异常链（exception chaining）**：Python 的 `raise X from Y` 会在新异常上保留原始异常 `Y`，打印时出现 "The above exception was the direct cause of..."，方便追根溯源。
- **主备 URL（primary / backup URL）**：同一份文件放在两个不同的对象存储/托管服务上（本项目主用 Hugging Face，备用 Backblaze B2），一个挂了换另一个，提高下载成功率。

回顾上一讲（u1-l2）：我们已经在虚拟环境里装好依赖并跑通了 `generate_simple.py`。本讲我们反过来问：那个脚本第一次运行时，权重文件是怎么来的？

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的行段 |
| --- | --- | --- |
| `reasoning_from_scratch/utils.py` | 通用文件下载器（不含任何 torch 代码，仅 `requests` + 标准库） | 全文 114 行，重点是 `download_file`（L53-L113）与 `_download_error_message`（L11-L50） |
| `reasoning_from_scratch/qwen3.py` | 模型定义文件，文件末尾附三个下载入口 | `download_qwen3_small`（L440-L457）、`download_qwen3_grpo_checkpoints`（L460-L493）、`download_qwen3_distill_checkpoints`（L496-L531）；L5 的 `from .utils import download_file` 是两者唯一关联 |
| `reasoning_from_scratch/ch03.py` | 下载入口的典型调用方 | `load_model_and_tokenizer`（L52-L90）演示 base/reasoning 两种调用姿势 |
| `troubleshooting.md` | 下载失败的排查文档 | "File Download Issues" 一节（L22 起）列出全部可手动打开的 URL |

一个值得注意的结构事实：`utils.py` 故意不依赖 torch，是一个可以单独复制的轻量工具；而 `qwen3.py` 虽然是模型文件，却把「下载入口」放在文件末尾，让 `qwen3.py` 成为全书统一的「权重获取」门面——后面章节（ch03.py、ch08.py、qwen3_batched.py 等）都只 import `download_qwen3_small`，从不直接碰 `download_file`。

## 4. 核心概念与源码讲解

### 4.1 download_file：通用文件下载器

#### 4.1.1 概念说明

`download_file` 是整个仓库唯一的网络下载函数，解决三个问题：

1. **大文件不能一口吞**：模型权重超过 1 GB，必须分块写入磁盘，否则内存吃不消。
2. **下载要可观察**：命令行里没有图形进度条，需要用回车符 `\r` 原地刷新一行百分比。
3. **重复运行要幂等**：读者会反复运行同一章的脚本，不能每次都重新下 1.4 GB——本地已有完整文件时直接跳过。

它的策略是「**主 URL 失败 → 试备用 URL → 都失败才报错**」的双通道设计，且缓存判定只认「本地文件大小与服务器 Content-Length 完全相等」这一种情况。注意：它**不做断点续传**——如果上一次下载到一半中断，本地留下一个不完整的文件，下次调用时会以 `"wb"` 模式（覆盖写）从头重新下载，而不是从断点接着写。

#### 4.1.2 核心流程

```text
download_file(url, out_dir, backup_url)
│
├─ 1. mkdir(out_dir)；从 url 解析出文件名，dest = out_dir/文件名
│
├─ 2. try_download(主 URL)
│     ├─ GET(stream=True, timeout=30)，raise_for_status() 检查 4xx/5xx
│     ├─ 读 Content-Length → size_remote
│     ├─ 缓存判定：dest 存在 且 size_remote>0 且 本地大小 == size_remote
│     │    → 打印 "✓ ... already up-to-date"，直接成功返回
│     ├─ 否则：以 1 MiB 为块循环写入 dest，实时打印百分比
│     └─ 任一环节抛 requests.RequestException → 捕获，返回 (False, exc)
│
├─ 3. 主 URL 失败且给了 backup_url → try_download(备用 URL)
│
└─ 4. 仍失败 → _download_error_message 组装报错
       → raise RuntimeError(message) from 原始异常
```

进度百分比的算法是整除：\( \text{pct} = \lfloor \text{downloaded} \times 100 \,/\, \text{size\_remote} \rfloor \)，所以显示为 0%~99%，写完最后一个块后循环结束，补一个换行符。

#### 4.1.3 源码精读

**第一步：确定目标路径。**

[reasoning_from_scratch/utils.py:L53-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L53-L57) 中，函数先用 `Path` 建好输出目录（`parents=True` 允许嵌套目录一键创建），再用 `urlparse` 从 URL 尾部截出文件名。好处是调用方不用单独传文件名，文件名永远和 URL 保持一致：

```python
def download_file(url, out_dir=".", backup_url=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(url).path).name
    dest = out_dir / filename
```

**第二步：`try_download` 内层函数——发起请求并做缓存跳过。**

[reasoning_from_scratch/utils.py:L59-L68](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L59-L68) 定义了闭包 `try_download`（它捕获外层的 `dest`），发请求后立刻做三件事：`raise_for_status()` 把 HTTP 404/500 变成异常；读 `Content-Length`；以及关键的缓存判定：

```python
def try_download(u):
    try:
        with requests.get(u, stream=True, timeout=30) as r:
            r.raise_for_status()
            size_remote = int(r.headers.get("Content-Length", 0))

            # Skip download if already complete
            if dest.exists() and size_remote and dest.stat().st_size == size_remote:
                print(f"✓ {dest} already up-to-date")
                return True, None
```

缓存条件是三个部分的**逻辑与**：文件存在、服务器报告了大小、且字节数严格相等。三个条件缺一不可——`size_remote` 为 0（服务器没给 Content-Length）时宁可重新下载，也不误判。

**第三步：分块下载与进度条。**

[reasoning_from_scratch/utils.py:L70-L88](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L70-L88) 以 1 MiB 为单位迭代响应体写入文件。`\r`（回车不换行）让每次刷新都覆盖同一行，形成进度条效果；`if not chunk: continue` 跳过 keep-alive 空块：

```python
block = 1024 * 1024
downloaded = 0
with open(dest, "wb") as f:
    for chunk in r.iter_content(chunk_size=block):
        if not chunk:
            continue
        f.write(chunk)
        downloaded += len(chunk)
        if size_remote:
            pct = downloaded * 100 // size_remote
            sys.stdout.write(
                f"\r{filename}: {pct:3d}% "
                f"({downloaded // (1024*1024)} MiB / "
                f"{size_remote // (1024*1024)} MiB)"
            )
            sys.stdout.flush()
```

**第四步：异常捕获与主备双通道。**

[reasoning_from_scratch/utils.py:L90-L104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L90-L104) 中，`except requests.RequestException` 统一兜住连接错误、超时、SSL 错误、HTTP 状态错误等所有 requests 异常，返回 `(False, exc)` 而不是直接抛——这就给了主 URL 失败后换备用 URL 的机会：

```python
    except requests.RequestException as exc:
        return False, exc

# Try main URL first
success, primary_error = try_download(url)
if success:
    return dest

# Try backup URL if provided
backup_error = None
if backup_url:
    print(f"Primary URL ({url}) failed.\nTrying backup URL ({backup_url})...")
    success, backup_error = try_download(backup_url)
    if success:
        return dest
```

**第五步：组装报错并抛出。**

[reasoning_from_scratch/utils.py:L106-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L106-L113) 调用下一节讲的 `_download_error_message` 生成信息，`raise ... from (backup_error or primary_error)` 把原始异常挂到异常链上（有备用异常优先挂备用，因为它更接近失败现场）：

```python
message = _download_error_message(
    filename=filename, url=url,
    primary_error=primary_error,
    backup_url=backup_url, backup_error=backup_error,
)
raise RuntimeError(message) from (backup_error or primary_error)
```

#### 4.1.4 代码实践

这是一个**离线阅读型实践**，帮你确认对缓存判定的理解（不需要网络）：

1. **实践目标**：验证「缓存判定只认大小完全相等」这一行为。
2. **操作步骤**：在你的虚拟环境里运行下面这段脚本（示例代码）：

   ```python
   # 示例代码：手工模拟"本地已有文件"的缓存判定逻辑
   from pathlib import Path

   size_remote = 1000          # 假设服务器 Content-Length 为 1000 字节

   for local_bytes in (1000, 999, 0):
       dest_exists = local_bytes > 0
       hit = dest_exists and size_remote and local_bytes == size_remote
       print(f"本地 {local_bytes:>4} 字节 -> 缓存命中: {hit}")
   ```

3. **需要观察的现象**：三行输出分别是 `True`、`False`、`False`。
4. **预期结果**：只有 1000 == 1000 命中；999 字节（半截文件）会触发重新下载，因为源码用 `"wb"` 覆盖写而非续传。完整结论待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `stream=True` 去掉，程序还能得到正确结果吗？会有什么问题？
**答案**：结果仍正确（`iter_content` 依旧可用），但 `requests` 会先在内部把整个响应体读进内存，分块写入失去意义；下载 1.4 GB 权重时内存占用会飙升，`timeout=30` 也只覆盖连接建立阶段，与设计意图相悖。

**练习 2**：为什么 `try_download` 捕获的是 `requests.RequestException` 而不是 `Exception`？
**答案**：`RequestException` 是 requests 库所有网络相关异常（`ConnectionError`、`Timeout`、`SSLError`、`HTTPError` 等）的公共基类，捕获它恰好覆盖「下载可能失败」的全部预期情形；捕获裸 `Exception` 会把键盘中断（`KeyboardInterrupt`）、磁盘写满（`OSError`）等无关错误也吞掉，导致该报错的没报。

**练习 3**：主 URL 和备用 URL 下载的是同一文件名，都写 `dest`。如果主 URL 下载到一半失败，备用 URL 成功，最终文件会正常吗？
**答案**：会正常。备用下载以 `open(dest, "wb")` 重新打开，先清空之前写了一半的内容再从头写入，最终 `dest` 是备用通道的完整文件。

### 4.2 _download_error_message：友好的失败诊断

#### 4.2.1 概念说明

下载失败时，直接把 `ConnectionError: HTTPSConnectionPool...` 这样的原始堆栈甩给初学者是非常糟糕的体验。`_download_error_message`（注意下划线前缀，表明它是模块内部函数）把两次尝试的错误整理成一段结构化文本，并做了一件很聪明的事：**自动识别 SSL/证书/代理类问题**。因为这类错误几乎总是发生在公司或学校的受管网络里（VPN、防火墙、杀毒软件劫持 HTTPS），读者自己很难想到，而提示一句话就能把排查方向从「代码写错了」扭转到「网络环境问题」。

#### 4.2.2 核心流程

```text
_download_error_message(filename, url, primary_error, backup_url, backup_error)
│
├─ 1. 首行："Failed to download {filename}."
├─ 2. 若有主 URL 错误   → 追加 "Primary URL failed (url): 异常类型: 异常内容"
├─ 3. 若有备用 URL 错误 → 追加 "Backup URL failed (url): 异常类型: 异常内容"
├─ 4. 证书/代理识别（两道防线）：
│     a. 异常类型是 ProxyError 或 SSLError → 命中
│     b. 否则把错误文本转小写，搜关键词 "certificate"/"ssl"/"tls"/"proxy" → 命中
│     命中则追加"VPN/代理/杀毒软件拦截证书"的说明
└─ 5. 永远追加 troubleshooting.md 的链接（含 'File Download Issues' 章节提示）
```

两道防线是「类型精确匹配」与「文本模糊匹配」的组合：有些代理错误包装成普通 `ConnectionError`，类型对不上，但错误消息里通常仍含 "proxy" 或 "certificate" 字样，第二道防线就能兜住。

#### 4.2.3 源码精读

**错误明细收集。** [reasoning_from_scratch/utils.py:L11-L24](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L11-L24) 用列表累积多行信息，`type(err).__name__` 把异常类名（如 `ConnectionError`）显式写进消息，帮助读者分辨失败种类：

```python
def _download_error_message(filename, url, primary_error, backup_url=None, backup_error=None):
    details = [f"Failed to download {filename}."]

    if primary_error is not None:
        details.append(
            f"Primary URL failed ({url}): "
            f"{type(primary_error).__name__}: {primary_error}"
        )
```

**证书/代理双保险识别。** [reasoning_from_scratch/utils.py:L26-L43](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L26-L43) 先按异常类型判断，再用关键词扫描兜底，命中后追加面向受管网络用户的提示语：

```python
cert_or_proxy_issue = any(
    isinstance(err, (requests.exceptions.ProxyError, requests.exceptions.SSLError))
    for err in (primary_error, backup_error)
    if err is not None
)
if not cert_or_proxy_issue:
    lowered = " ".join(
        str(err).lower() for err in (primary_error, backup_error) if err is not None
    )
    cert_or_proxy_issue = any(
        keyword in lowered for keyword in ("certificate", "ssl", "tls", "proxy")
    )

if cert_or_proxy_issue:
    details.append(
        "This can happen on work or school machines where a VPN, proxy, or "
        "antivirus tool intercepts HTTPS certificates."
    )
```

**固定追加排查文档链接。** [reasoning_from_scratch/utils.py:L45-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L45-L50) 无论何种错误，最后都指向仓库内的 troubleshooting 文档并用 `"\n".join` 拼成多行消息——这就是本讲实践任务要验证的那条链接的出处：

```python
details.append(
    "See the troubleshooting guide: "
    "https://github.com/rasbt/reasoning-from-scratch/blob/main/troubleshooting.md "
    "(especially the 'File Download Issues' section)."
)
return "\n".join(details)
```

该文档的 "File Download Issues" 一节（[troubleshooting.md:L22-L34](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L22-L34)）逐一列出了所有可手动在浏览器打开核对的下载 URL，与本节讲的三个下载入口一一对应。

#### 4.2.4 代码实践

这是一个**离线可运行实践**（`_download_error_message` 是纯字符串处理函数，不需要网络）：

1. **实践目标**：亲眼看到两类错误信息长什么样，验证 troubleshooting 链接总会出现。
2. **操作步骤**：运行以下脚本（示例代码）：

   ```python
   # 示例代码：直接调用内部错误信息函数，观察输出
   from reasoning_from_scratch.utils import _download_error_message

   # 场景 A：普通连接错误
   msg_a = _download_error_message(
       filename="test.pth",
       url="https://example.invalid/test.pth",
       primary_error=ConnectionError("HTTPSConnectionPool: Max retries exceeded"),
   )
   print(msg_a)
   print("=" * 60)

   # 场景 B：错误文本里带 "proxy" 关键词
   msg_b = _download_error_message(
       filename="test.pth",
       url="https://example.invalid/test.pth",
       primary_error=RuntimeError("Cannot connect to proxy 10.0.0.1"),
   )
   print(msg_b)
   ```

3. **需要观察的现象**：场景 A 输出三行（失败声明、Primary URL failed、troubleshooting 链接）；场景 B 额外多一行 VPN/proxy/antivirus 提示。
4. **预期结果**：两段输出都以 "See the troubleshooting guide: ..." 结尾；只有场景 B 包含 "work or school machines"。输出细节待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：场景 B 中传入的异常是 `RuntimeError`，既不是 `ProxyError` 也不是 `SSLError`，为什么仍被识别为证书/代理问题？
**答案**：第一道类型防线没命中，但第二道防线把错误文本 `"Cannot connect to proxy 10.0.0.1"` 转小写后扫描关键词，命中了 `"proxy"`，于是判定成立。

**练习 2**：为什么这个函数以下划线开头？
**答案**：Python 约定单下划线前缀表示「模块内部实现」，不希望被外部直接 import。它只是 `download_file` 的报错辅助，调用入口应当永远是 `download_file` / 三个 download_qwen3_* 函数。

**练习 3**：如果 `primary_error` 与 `backup_error` 都是 `None`（两次都"成功"了却走到报错分支），输出会是什么？
**答案**：从代码结构看不可能发生（成功即 return），但若真发生，输出只有首行 "Failed to download ..." 和 troubleshooting 链接两行——函数对 `None` 做了判空保护，不会崩溃。

### 4.3 download_qwen3_small：base/reasoning 两套权重入口

#### 4.3.1 概念说明

`download_qwen3_small` 是全书使用频率最高的下载入口，负责把第 2~5 章需要的 Qwen3 0.6B 文件拉到本地。它支持两种 `kind`：

| kind | 模型权重 | 分词器 | 用途 |
| --- | --- | --- | --- |
| `"base"` | `qwen3-0.6B-base.pth` | `tokenizer-base.json` | 预训练基座模型（第 2~4 章默认） |
| `"reasoning"` | `qwen3-0.6B-reasoning.pth` | `tokenizer-reasoning.json` | 官方推理变体（对照实验用） |

两套文件都存放在 Hugging Face 仓库 `rasbt/qwen3-from-scratch`（主通道），并在 Backblaze B2 上有完整镜像（备用通道）。`tokenizer_only=True` 时跳过 1 GB 级的模型权重、只下几 MB 的分词器——当你只想做文本编码实验时能省大量时间与流量。

#### 4.3.2 核心流程

```text
download_qwen3_small(kind, tokenizer_only, out_dir)
│
├─ 1. files 字典：kind → {model: 文件名, tokenizer: 文件名}
├─ 2. kind 不在字典中 → ValueError（如 kind=" Base" 带空格也会被拒）
├─ 3. 拼出主 URL 模板（Hugging Face）与备用根 URL（Backblaze B2）
├─ 4. targets = ["tokenizer"]（tokenizer_only=True）
│          或 ["model", "tokenizer"]（默认，两个都下）
└─ 5. 对每个目标调用 download_file(primary, out_dir, backup_url=backup)
```

注意此函数**没有返回值**（循环体内调 `download_file` 但不 return），而后面两个 checkpoint 函数会返回 `download_file` 的结果（本地文件路径）。这是源码里一处微妙的不对称。

#### 4.3.3 源码精读

[reasoning_from_scratch/qwen3.py:L440-L457](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L440-L457) 是完整实现。`files` 字典同时充当「合法 kind 白名单」与「文件名查表」；`hf_fmt` 用 `str.format` 生成主 URL；`targets` 列表决定循环次数：

```python
def download_qwen3_small(kind="base", tokenizer_only=False, out_dir="."):
    files = {
        "base": {"model": "qwen3-0.6B-base.pth", "tokenizer": "tokenizer-base.json"},
        "reasoning": {"model": "qwen3-0.6B-reasoning.pth", "tokenizer": "tokenizer-reasoning.json"},
    }
    if kind not in files:
        raise ValueError("kind must be 'base' or 'reasoning'")

    repo = "rasbt/qwen3-from-scratch"
    hf_fmt = "https://huggingface.co/{repo}/resolve/main/{file}"
    backup_root = "https://f001.backblazeb2.com/file/reasoning-from-scratch/qwen3-0.6B"
    targets = ["tokenizer"] if tokenizer_only else ["model", "tokenizer"]

    for key in targets:
        fname = files[kind][key]
        primary = hf_fmt.format(repo=repo, file=fname)
        backup = f"{backup_root}/{fname}"
        download_file(primary, out_dir=out_dir, backup_url=backup)
```

与 `utils.py` 的连接点在 [reasoning_from_scratch/qwen3.py:L5](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L5)：`from .utils import download_file`，一行相对导入，说明下载能力被有意抽离出了模型文件。

**典型调用方**长什么样？看 [reasoning_from_scratch/ch03.py:L52-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L70) 的 `load_model_and_tokenizer`：先无条件调用下载（已下载则秒级跳过），再按固定文件名拼路径加载——「下载保证存在，路径写死约定」是全书统一的模式：

```python
def load_model_and_tokenizer(which_model, device, use_compile, local_dir="qwen3"):
    if which_model == "base":
        download_qwen3_small(
            kind="base", tokenizer_only=False, out_dir=local_dir
        )
        tokenizer_path = Path(local_dir) / "tokenizer-base.json"
        model_path = Path(local_dir) / "qwen3-0.6B-base.pth"
```

另一个真实调用来自 [ch08/05_download_training_checkpoints/usage_example.py:L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L25)：`download_qwen3_small(kind="reasoning", tokenizer_only=True, out_dir=local_dir)`——正是 `tokenizer_only` 分支的官方用法示范。

#### 4.3.4 代码实践

**源码阅读型实践**（不需要网络）：

1. **实践目标**：不用运行，手推两种调用各自会发起几次下载、URL 是什么。
2. **操作步骤**：在纸上或注释里回答：
   - `download_qwen3_small(kind="base")` 会发起几次 `download_file` 调用？各自的 URL？
   - `download_qwen3_small(kind="reasoning", tokenizer_only=True)` 呢？
3. **需要观察的现象**：与源码 L451-L457 逐行核对。
4. **预期结果**：前者 2 次，先 `https://huggingface.co/rasbt/qwen3-from-scratch/resolve/main/qwen3-0.6B-base.pth`，再 `.../tokenizer-base.json`，且都带 Backblaze 备用 URL；后者 1 次，只下载 `.../tokenizer-reasoning.json`。

#### 4.3.5 小练习与答案

**练习 1**：调用 `download_qwen3_small(kind="Base")`（首字母大写）会发生什么？
**答案**：`"Base" not in files` 成立，抛出 `ValueError: kind must be 'base' or 'reasoning'`。`files` 字典的键就是精确白名单，不做大小写归一化。

**练习 2**：为什么模型和分词器要分成两套（base/reasoning）而不是共用一个 tokenizer？
**答案**：两套模型虽然词表相同，但官方对 base 与推理变体配置的收尾 token（eos）与聊天模板行为不同，项目把差异固化到两个 tokenizer JSON 里（详见 u2-l6 讲 Qwen3Tokenizer 时展开），下载时必须配套，不能混用。

**练习 3**：`download_qwen3_small` 与 `download_qwen3_grpo_checkpoints` 在返回值上有何不同？
**答案**：前者无返回值（隐式返回 `None`），后者把 `download_file` 的返回值 `dest`（本地 Path）透传给调用方，方便调用方直接拿着路径去 `torch.load`。

### 4.4 checkpoint 下载入口：GRPO 与蒸馏（拓展模块）

#### 4.4.1 概念说明

第 7、8 章训练产出的中间检查点（checkpoint），作者已上传到两个独立 HF 仓库，本仓库提供两个函数按需下载：

- `download_qwen3_grpo_checkpoints`：第 7 章各种 GRPO 训练变体在第 N 步的模型权重，用于直接复用或对照分析训练效果。
- `download_qwen3_distill_checkpoints`：第 8 章两种教师模型蒸馏出的检查点（deepseek_r1 / qwen3_235b_a22b 两个来源，各 3 个 epoch）。

两者的共同套路是「**字典做参数校验 + 模板拼 URL**」：合法取值写死在 mapper 字典里，传入不支持的值立刻 `ValueError`，而不是让错误 URL 去网络上撞 404。这比 `download_qwen3_small` 的双键字典更进一步——校验与文件名映射合为一体。

#### 4.4.2 核心流程

```text
download_qwen3_grpo_checkpoints(grpo_type, step, out_dir)
│
├─ 1. mapper：5 种 grpo_type → HF 仓库子目录
├─ 2. step 转字符串，若纯数字则 zfill(5) 补零（"50" → "00050"）
├─ 3. 文件名 = qwen3-0.6B-rlvr-grpo-step{step}.pth
├─ 4. 主 URL = HF 仓库 rasbt/qwen3-from-scratch-grpo-checkpoints/子目录/文件名
├─ 5. 仅当 grpo_type=="no_kl" 且 step=="00050" 时提供 Backblaze 备用 URL
└─ 6. return download_file(primary, out_dir, backup_url=backup)

download_qwen3_distill_checkpoints(distill_type, step, out_dir)
│
├─ 1. mapper：2 种 distill_type × 3 个 step → 完整文件名（校验 step 是否在表内）
├─ 2. folder_map：distill_type → HF 仓库子目录
├─ 3. 主 URL = rasbt/qwen3-from-scratch-distill-checkpoints/子目录/文件名
└─ 4. return download_file(primary, out_dir)   # 注意：无备用 URL
```

#### 4.4.3 源码精读

**GRPO 检查点。** [reasoning_from_scratch/qwen3.py:L460-L493](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L460-L493) 中，`zfill(5)` 把用户随手传的 `50` 规范成 `00050`，与文件名对齐；备用 URL 只为最常用的 `no_kl/00050` 组合配置，其余仅走 HF 单通道：

```python
mapper = {
    "no_kl": "grpo_original_no_kl",
    "tracking": "7_3_plus_tracking/checkpoints",
    "clip_ratio": "7_4_plus_clip_ratio/checkpoints",
    "kl": "7_5_plus_kl/checkpoints",
    "format_reward": "7_6_plus_format_reward/checkpoints",
}
...
step = str(step)
if step.isdigit():
    step = step.zfill(5)
fname = f"qwen3-0.6B-rlvr-grpo-step{step}.pth"
primary = f"https://huggingface.co/{repo}/resolve/main/{mapper[grpo_type]}/{fname}"

backup = None
if grpo_type == "no_kl" and step == "00050":
    ...
```

**蒸馏检查点。** [reasoning_from_scratch/qwen3.py:L496-L531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L496-L531) 中，step 不仅被补零，还要在 mapper 表里存在才放行（双重校验），且最后调用 `download_file` 时不传 `backup_url`：

```python
mapper = {
    "deepseek_r1": {
        "06682": "qwen3-0.6B-distill-step06682-epoch1.pth",
        "13364": "qwen3-0.6B-distill-step13364-epoch2.pth",
        "20046": "qwen3-0.6B-distill-step20046-epoch3.pth",
    },
    ...
}
if step not in mapper[distill_type]:
    raise ValueError(
        f"only step in {mapper[distill_type].keys()} are supported for {distill_type}"
    )
...
return download_file(primary, out_dir=out_dir)
```

三个入口对照如下：

| 入口 | 主仓库 | 备用通道 | 返回值 |
| --- | --- | --- | --- |
| `download_qwen3_small` | rasbt/qwen3-from-scratch | 每个文件都有（Backblaze） | `None` |
| `download_qwen3_grpo_checkpoints` | rasbt/qwen3-from-scratch-grpo-checkpoints | 仅 no_kl + step 00050 | 本地文件 `Path` |
| `download_qwen3_distill_checkpoints` | rasbt/qwen3-from-scratch-distill-checkpoints | 无 | 本地文件 `Path` |

#### 4.4.4 代码实践

**源码阅读型实践**（权重文件较大，本实践不实际下载）：

1. **实践目标**：手推 URL 拼装逻辑，验证对 zfill 与 mapper 的理解。
2. **操作步骤**：在 Python 里只做字符串运算（示例代码，不发任何网络请求）：

   ```python
   # 示例代码：手工复现 URL 拼装，验证与源码一致
   step = str(50)
   step = step.zfill(5) if step.isdigit() else step
   mapper = {"kl": "7_5_plus_kl/checkpoints"}
   fname = f"qwen3-0.6B-rlvr-grpo-step{step}.pth"
   url = f"https://huggingface.co/rasbt/qwen3-from-scratch-grpo-checkpoints/resolve/main/{mapper['kl']}/{fname}"
   print(url)
   ```

3. **需要观察的现象**：打印出的 URL 中 step 是否已被补零成 `00050`。
4. **预期结果**：`https://huggingface.co/rasbt/qwen3-from-scratch-grpo-checkpoints/resolve/main/7_5_plus_kl/checkpoints/qwen3-0.6B-rlvr-grpo-step00050.pth`。可进一步用浏览器打开该 URL 核对存在性（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：调用 `download_qwen3_grpo_checkpoints(grpo_type="kl", step=50)` 时会配置备用 URL 吗？
**答案**：不会。备用 URL 仅在 `grpo_type == "no_kl" and step == "00050"` 时给出（L483），`"kl"` 走 HF 单通道；`step=50` 先被补零成 `"00050"`，但 grpo_type 条件不满足。

**练习 2**：`download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06681")` 会发生什么？
**答案**：`"06681".isdigit()` 为真、补零后仍是 `"06681"`（已满 5 位），但它不在 `mapper["deepseek_r1"]` 的键中，抛出 `ValueError: only step in dict_keys(['06682', '13364', '20046']) are supported for deepseek_r1`——错误在本地立即发生，而非网络 404。

**练习 3**：为什么 GRPO 只给最常用的一个组合配备用 URL，而 `download_qwen3_small` 每个文件都配？
**答案**：主备双通道维护的是镜像存储成本。0.6B 模型/分词器是所有读者的必经之路，配全量镜像收益最大；GRPO 检查点有 5 类 × 多个 step，镜像全量成本高、需求分散，只为最常用的默认组合（`no_kl` + `00050`）保底即可。

## 5. 综合实践

把本讲三个模块串成一次完整的「下载体验」：**只下载 tokenizer → 验证缓存命中 → 触发一次带排查指引的失败**。

1. **实践目标**：完整走一遍 `download_qwen3_small` 的正常路径与 `download_file` 的失败路径，看到 `✓ already up-to-date` 与 troubleshooting 链接两种标志性输出。

2. **操作步骤**：

   在仓库根目录激活你的虚拟环境后，创建脚本 `explore_download.py`（示例代码，建议放在教程目录或任意临时目录，不要放进 `reasoning_from_scratch` 包内）：

   ```python
   # 示例代码：u1-l3 综合实践
   from reasoning_from_scratch.qwen3 import download_qwen3_small
   from reasoning_from_scratch.utils import download_file

   # ── 第 1 步：只下载 base 分词器（几 MB，不需要 GPU）──
   download_qwen3_small(kind="base", tokenizer_only=True, out_dir="qwen3")

   # ── 第 2 步：再次调用同一函数，观察缓存命中 ──
   download_qwen3_small(kind="base", tokenizer_only=True, out_dir="qwen3")

   # ── 第 3 步：用无效 URL 触发报错（404，失败很快）──
   try:
       download_file(
           "https://huggingface.co/rasbt/qwen3-from-scratch/resolve/main/no-such-file.pth",
           out_dir="qwen3",
       )
   except RuntimeError as e:
       print("\n--- 捕获到 RuntimeError，报错内容如下 ---")
       print(e)
   ```

   运行：`python explore_download.py`

3. **需要观察的现象**：
   - 第 1 次调用：终端出现 `tokenizer-base.json: xx% (...)` 样式的进度行，结束后 `qwen3/` 目录下多出 `tokenizer-base.json`；
   - 第 2 次调用：几乎瞬时返回，打印 `✓ qwen3/tokenizer-base.json already up-to-date`；
   - 第 3 步：先打印 `Primary URL (...) failed.`（此例无备用 URL 则直接进入报错），最终抛出的 `RuntimeError` 消息以 `See the troubleshooting guide: https://github.com/rasbt/reasoning-from-scratch/blob/main/troubleshooting.md ...` 结尾。

4. **预期结果**：缓存命中的精确输出为 `✓ qwen3/tokenizer-base.json already up-to-date`（对应 [utils.py:L67](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L67) 的 f-string）；404 报错中会出现 `Primary URL failed (...): HTTPError: 404 Client Error ...`（对应 [utils.py:L14-L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L14-L18)）。进度百分比、MiB 数值与报错措辞细节依赖网络环境，待本地验证。

5. **延伸（可选）**：把第 3 步的 URL 换成一个不存在的域名（如 `https://example.invalid/x.pth`），对比 `ConnectionError` 与 `HTTPError` 两种 `Primary URL failed` 行文的差异；再打开 [troubleshooting.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md) 的 "File Download Issues" 一节，确认报错里推荐的每个 URL 都能在浏览器手动打开。

## 6. 本讲小结

- `download_file`（[utils.py:L53-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L53-L113)）是全仓库唯一的下载原语：流式 1 MiB 分块写盘、`\r` 原地刷新进度条、主 URL 失败自动切换备用 URL，缓存判定只认「本地大小 == Content-Length」，不做断点续传。
- `_download_error_message`（[utils.py:L11-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L11-L50)）把网络异常翻译成结构化报错，通过「异常类型 + 错误文本关键词」双保险识别 SSL/代理问题，并永远附上 troubleshooting.md 链接。
- `download_qwen3_small`（[qwen3.py:L440-L457](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L440-L457)）以 `kind` 选 base/reasoning 两套权重+分词器，`tokenizer_only=True` 只拉分词器；它是 ch03 等章节 `load_model_and_tokenizer` 的底层依赖。
- 两个 checkpoint 入口（[qwen3.py:L460-L493](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L460-L493)、[qwen3.py:L496-L531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L496-L531)）展示「mapper 字典做本地参数校验 + 模板拼 URL」的模式，非法参数在本地立即 ValueError 而非网上 404。
- `utils.py` 刻意零依赖 torch，可独立复用；`qwen3.py` 则把下载入口收拢为全书门面，上层代码从不直接调用 `download_file`。

## 7. 下一步学习建议

- 下一讲（u2-l1「Qwen3 模型总体架构」）将第一次真正加载本讲下载的 `.pth` 权重：从 `QWEN_CONFIG_06_B` 配置字典和 `Qwen3Model.forward` 入手，纵览 embedding → 28 层 TransformerBlock → final_norm → out_head 的数据流。届时你会看到 `torch.load(model_path)` 与本讲 `dest` 路径的衔接点。
- 若想继续在本讲的延长线上深挖，建议阅读：`reasoning_from_scratch/qwen3_optimized.py` 与 `reasoning_from_scratch/qwen3_batched.py` 中各自复制的 `download_qwen3_small`（用 `Grep` 搜 `def download_qwen3_small`），思考「为什么优化版要复制一份而不是 import」；以及 `troubleshooting.md` 的 "Corrupted Checkpoints" 一节（第 6 章），它处理的是下载成功但文件损坏的情形——与本讲的「大小校验」互补。
- 动手型读者可以提前做一个思考实验：把综合实践脚本里的 `out_dir` 从 `"qwen3"` 改成别的目录再运行，验证缓存判定是**按路径**而非按文件名生效的——这能加深你对 `dest = out_dir / filename` 的理解。
