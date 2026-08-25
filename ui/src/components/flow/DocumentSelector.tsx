"use client";

import { FileText } from "lucide-react";
import { useMemo } from "react";

import type { DocumentResponseSchema } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { KNOWLEDGE_BASE_DOC_URL } from "@/constants/documentation";

interface DocumentSelectorProps {
    value: string[];
    onChange: (uuids: string[]) => void;
    documents: DocumentResponseSchema[];
    disabled?: boolean;
    label?: string;
    description?: string;
    showLabel?: boolean;
}

export const DocumentSelector = ({
    value,
    onChange,
    documents,
    disabled = false,
    label = "Knowledge Base Documents",
    description = "Select documents that the agent can reference during conversations.",
    showLabel = true,
}: DocumentSelectorProps) => {
    // Only show completed documents
    const completedDocuments = useMemo(
        () => documents.filter((doc) => doc.processing_status === "completed"),
        [documents]
    );

    const handleToggle = (documentUuid: string, checked: boolean) => {
        if (checked) {
            onChange([...value, documentUuid]);
        } else {
            onChange(value.filter((uuid) => uuid !== documentUuid));
        }
    };

    const formatFileSize = (bytes: number): string => {
        if (bytes === 0) return "0 Bytes";
        const k = 1024;
        const sizes = ["Bytes", "KB", "MB", "GB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + " " + sizes[i];
    };

    if (completedDocuments.length === 0) {
        return (
            <div className="space-y-2">
                {showLabel && (
                    <>
                        <Label>{label}</Label>
                        {description && (
                            <Label className="text-xs text-muted-foreground">
                            {description}{" "}
                            <a href={KNOWLEDGE_BASE_DOC_URL} target="_blank" rel="noopener noreferrer" className="underline">Learn more</a>
                        </Label>
                        )}
                    </>
                )}
                <div className="border rounded-md p-4 space-y-3">
                    <div className="text-sm text-muted-foreground text-center">
                        No documents available. Upload documents to the knowledge base first.
                    </div>
                    {/* customer-center-platform fork（母 repo W2d task 3.6）：
                        上游的「Upload Documents」連到 `/files`，該頁在閘門的 UI 拒絕
                        清單內 ⇒ 開新分頁只會拿到 403。依 task 3.2b，**入口本身通往
                        不可達頁面**是「移除」而非「停用」的唯一合格情形，故移除。
                        知識庫在本部署是唯讀（僅清單與 metadata），文件由建置單位匯入。 */}
                </div>
            </div>
        );
    }

    return (
        <div className="space-y-2">
            {showLabel && (
                <>
                    <Label>{label}</Label>
                    {description && (
                        <Label className="text-xs text-muted-foreground">
                            {description}{" "}
                            <a href={KNOWLEDGE_BASE_DOC_URL} target="_blank" rel="noopener noreferrer" className="underline">Learn more</a>
                        </Label>
                    )}
                </>
            )}
            <div className="border rounded-md max-h-[300px] overflow-y-auto">
                <div className="divide-y">
                    {completedDocuments.map((doc) => (
                        <div
                            key={doc.document_uuid}
                            className="flex items-start gap-3 p-3 hover:bg-muted/50 transition-colors"
                        >
                            <Checkbox
                                id={`doc-${doc.document_uuid}`}
                                checked={value.includes(doc.document_uuid)}
                                onCheckedChange={(checked) =>
                                    handleToggle(doc.document_uuid, checked as boolean)
                                }
                                disabled={disabled}
                            />
                            <div className="flex-1 space-y-1">
                                <label
                                    htmlFor={`doc-${doc.document_uuid}`}
                                    className="flex items-center gap-2 cursor-pointer"
                                >
                                    <div className="w-8 h-8 rounded-md bg-blue-500/10 flex items-center justify-center flex-shrink-0">
                                        <FileText className="w-4 h-4 text-blue-500" />
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="text-sm font-medium truncate">
                                            {doc.filename}
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                            {formatFileSize(doc.file_size_bytes)} • {doc.retrieval_mode === 'full_document' ? 'Full Document' : `${doc.total_chunks} chunks`}
                                        </div>
                                    </div>
                                </label>
                            </div>
                        </div>
                    ))}
                </div>
                {/* customer-center-platform fork（母 repo W2d task 3.6）：
                    「Manage Documents」同樣連到 `/files`（不可達）——同批移除。
                    完整的空狀態文案歸 W4。 */}
            </div>

            {value.length > 0 && (
                <p className="text-xs text-muted-foreground">
                    {value.length} {value.length === 1 ? "document" : "documents"} selected
                </p>
            )}
        </div>
    );
};
