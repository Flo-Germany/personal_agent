import { memo } from "react";
import { Files, Images } from "lucide-react";

import { FileReference } from "./FileReference";
import { isImagePath } from "../lib/localFiles";

type AttachmentBlockProps = {
  files: string[];
};

export const AttachmentBlock = memo(function AttachmentBlock({
  files,
}: AttachmentBlockProps) {
  if (files.length === 0) {
    return null;
  }

  const allImages = files.every((path) => isImagePath(path));
  const Icon = allImages ? Images : Files;
  const label = allImages
    ? files.length === 1
      ? "image"
      : "images"
    : files.length === 1
      ? "file"
      : "files";

  return (
    <div className="message-in">
      <div className="file-gallery-panel">
        <div className="file-gallery-heading">
          <Icon className="size-3" />
          <span>
            {files.length} {label}
          </span>
        </div>
        <div className={allImages ? "image-gallery" : "file-grid"}>
          {files.map((path) => (
            <FileReference key={path} path={path} />
          ))}
        </div>
      </div>
    </div>
  );
});
