"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";
import { TextField } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { errorMessage } from "@/lib/api";
import { createWorkspace, workspaceKeys, workspaceNameSchema } from "@/lib/workspaces";

interface CreateWorkspaceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CreateWorkspaceDialog({ open, onOpenChange }: CreateWorkspaceDialogProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [nameError, setNameError] = useState<string | undefined>();

  const create = useMutation({
    mutationFn: createWorkspace,
    onSuccess: async (workspace) => {
      await queryClient.invalidateQueries({ queryKey: workspaceKeys.all });
      onOpenChange(false);
      router.push(`/dashboard/workspaces/${workspace.id}`);
    },
  });

  function handleOpenChange(next: boolean) {
    if (!next) {
      setNameError(undefined);
      create.reset();
    }
    onOpenChange(next);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = workspaceNameSchema.safeParse(new FormData(event.currentTarget).get("name"));
    if (!parsed.success) {
      setNameError(parsed.error.issues[0]?.message);
      return;
    }
    setNameError(undefined);
    create.mutate(parsed.data);
  }

  return (
    <Modal
      open={open}
      onOpenChange={handleOpenChange}
      title="Create workspace"
      description="A workspace keeps a set of documents and the people who can use them together."
    >
      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
        {create.isError && <Alert>{errorMessage(create.error)}</Alert>}
        <TextField
          label="Workspace name"
          name="name"
          placeholder="e.g. Research"
          maxLength={100}
          autoComplete="off"
          error={nameError}
          disabled={create.isPending}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => handleOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending}>
            {create.isPending ? "Creating…" : "Create workspace"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
