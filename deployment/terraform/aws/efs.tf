resource "aws_efs_file_system" "main" {
  creation_token   = local.name
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = local.tags
}

resource "aws_efs_mount_target" "main" {
  count           = length(aws_subnet.public)
  file_system_id  = aws_efs_file_system.main.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "main" {
  file_system_id = aws_efs_file_system.main.id

  posix_user {
    gid = 10001
    uid = 10001
  }

  root_directory {
    path = "/bastion"
    creation_info {
      owner_gid   = 10001
      owner_uid   = 10001
      permissions = "755"
    }
  }

  tags = local.tags
}
