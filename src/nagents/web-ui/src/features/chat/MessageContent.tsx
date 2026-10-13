import { createElement, Fragment, memo, useMemo, type ReactNode } from "react";
import { decodeHTMLStrict } from "entities";
import { Lexer, type MarkedToken, type Token, type Tokens } from "marked";
import { CodeBlock } from "../../components/CodeBlock.js";
import { safeHttpHref } from "../../components/TextLinks.js";

const headings = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;

function renderListItem(item: Tokens.ListItem, key: number): ReactNode {
  return <li key={key} className={item.task ? "markdown-task-item" : undefined}>
    {renderTokens(item.tokens, !item.loose)}
  </li>;
}

function renderToken(token: MarkedToken, key: number, tight: boolean, inLink: boolean): ReactNode {
  switch (token.type) {
    case "space":
    case "def":
      return null;
    case "heading":
      return createElement(headings[token.depth - 1] || "h6", { key }, renderTokens(token.tokens));
    case "paragraph":
      return tight ? <Fragment key={key}>{renderTokens(token.tokens)}</Fragment>
        : <p key={key}>{renderTokens(token.tokens)}</p>;
    case "text":
      return <Fragment key={key}>{token.tokens ? renderTokens(token.tokens, false, inLink) : token.escaped ? token.text : decodeHTMLStrict(token.text)}</Fragment>;
    case "escape":
      return <Fragment key={key}>{token.text}</Fragment>;
    case "strong":
      return <strong key={key}>{renderTokens(token.tokens, false, inLink)}</strong>;
    case "em":
      return <em key={key}>{renderTokens(token.tokens, false, inLink)}</em>;
    case "del":
      return <del key={key}>{renderTokens(token.tokens, false, inLink)}</del>;
    case "codespan":
      return <code key={key}>{token.text}</code>;
    case "br":
      return <br key={key} />;
    case "hr":
      return <hr key={key} />;
    case "blockquote":
      return <blockquote key={key}>{renderTokens(token.tokens)}</blockquote>;
    case "list": {
      const items = token.items.map(renderListItem);
      return token.ordered ? <ol key={key} start={Number(token.start) || 1}>{items}</ol> : <ul key={key}>{items}</ul>;
    }
    case "list_item":
      return renderListItem(token, key);
    case "checkbox":
      return <input key={key} type="checkbox" disabled checked={token.checked}
        aria-label={token.checked ? "Completed task" : "Incomplete task"} />;
    case "code": {
      const language = token.lang?.trim().split(/\s+/, 1)[0] || "";
      // A terminal newline matches Markdown code-block semantics without changing indentation.
      const code = token.text ? `${token.text.replace(/\n$/, "")}\n` : "";
      return <CodeBlock key={key} language={language} label={language ? `Code (${language})` : "Code"} text={code} />;
    }
    case "link": {
      const href = safeHttpHref(token.autolink ? token.href : decodeHTMLStrict(token.href));
      const children = token.autolink ? token.text : renderTokens(token.tokens, false, true);
      return href && !inLink ? <a key={key} href={href} title={token.title ? decodeHTMLStrict(token.title) : undefined}
        target="_blank" rel="noopener noreferrer">{children}</a> : <Fragment key={key}>{children}</Fragment>;
    }
    case "image": {
      // Markdown must never fetch model-supplied URLs. Attachments have their own ngn delivery path.
      const href = safeHttpHref(decodeHTMLStrict(token.href));
      const label = token.text ? decodeHTMLStrict(token.text) : "Image";
      return href && !inLink ? <a key={key} className="markdown-image-link" href={href}
        title="Open image" target="_blank" rel="noopener noreferrer">{label}</a>
        : <span key={key}>{label}</span>;
    }
    case "table":
      return <div key={key} className="markdown-table-scroll" role="region" aria-label="Table" tabIndex={0}>
        <table><thead><tr>{token.header.map((cell, index) => <th key={index} scope="col"
          style={{ textAlign: cell.align || undefined }}>{renderTokens(cell.tokens)}</th>)}</tr></thead>
          <tbody>{token.rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}
            style={{ textAlign: cell.align || undefined }}>{renderTokens(cell.tokens)}</td>)}</tr>)}</tbody>
        </table>
      </div>;
    case "html":
      // React escapes the literal source, including scripts, styles, iframes and resource URLs.
      return token.block ? <div key={key} className="markdown-literal">{token.text}</div>
        : <span key={key} className="markdown-literal">{token.text}</span>;
  }
}

function renderTokens(tokens: Token[], tight = false, inLink = false): ReactNode[] {
  // This private Lexer has no extensions, so it produces only Marked's built-in token union.
  // Position keys preserve a growing block's DOM node while a streamed response is appended.
  return tokens.map((token, key) => renderToken(token as MarkedToken, key, tight, inLink));
}

export const MessageContent = memo(function MessageContent({ text }: { text: string }) {
  const content = useMemo(() => {
    try {
      return renderTokens(Lexer.lex(text, { gfm: true, breaks: false }));
    } catch {
      // Pathological input must not take down the conversation. Keep it readable as literal text.
      return <span className="markdown-literal">{text}</span>;
    }
  }, [text]);
  return <div className="entry-content markdown-content">{content}</div>;
});
