'use client';

import { FolderPlus } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { toast } from 'sonner';

import { createFolderApiV1FolderPost } from '@/client/sdk.gen';
import { Button } from '@/components/ui/button';
// customer-center-platform fork（母 repo W2d task 3.1）：`POST /folder/` 帶
// `roles: [implementer]` ⇒ 對主管一律 403。停用而非隱藏（task 3.2b）。
import { useCcpReadOnly } from '@/lib/ccp/access';
import { ccpDisabledProps } from '@/lib/ccp/notice-bar';

import { FolderFormDialog } from './FolderFormDialog';

export function CreateFolderButton() {
    const router = useRouter();
    const [isOpen, setIsOpen] = useState(false);
    const readOnly = useCcpReadOnly();

    const handleCreate = async (name: string) => {
        const response = await createFolderApiV1FolderPost({ body: { name } });
        if (response.error) {
            // 409 = duplicate name; surface the server's message when present.
            const detail =
                (response.error as { detail?: string })?.detail ??
                'Failed to create folder';
            toast.error(detail);
            throw new Error(detail);
        }
        toast.success(`Folder "${name}" created`);
        router.refresh();
    };

    return (
        <>
            <Button
                variant="outline"
                onClick={() => setIsOpen(true)}
                {...ccpDisabledProps(readOnly)}
            >
                <FolderPlus className="w-4 h-4 mr-2" />
                New Folder
            </Button>
            <FolderFormDialog
                open={isOpen}
                onOpenChange={setIsOpen}
                title="Create folder"
                submitLabel="Create"
                onSubmit={handleCreate}
            />
        </>
    );
}
